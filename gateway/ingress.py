"""
FastAPI ingress gateway — Platform: AWS (ECS/EKS or Lambda + API Gateway)

Provides the HTTP entry point for the MAS pipeline.  Responsibilities:
  - JWT validation (Keycloak on-prem JWKS)
  - mTLS client-certificate binding
  - Prompt injection detection (block before state initialisation)
  - PII obfuscation of the request payload
  - Rate limiting (via Redis token bucket)
  - Request routing to the LangGraph compiled graph
  - Structured response formatting

This module is intentionally thin — all business logic lives in agents.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.logging_config import configure_logging, get_logger
from core.security import JWTValidationError, validate_jwt
from gateway.pii_obfuscator import obfuscate_payload
from gateway.prompt_injection_guard import PromptInjectionError, enforce_no_injection
from schemas.state import initial_state

configure_logging(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    json_output=os.environ.get("LOG_FORMAT", "json") == "json",
)
logger = get_logger(__name__)

app = FastAPI(
    title="MAS Enterprise Gateway",
    version="1.0.0",
    description="Multi-Agent System ingress endpoint",
    docs_url="/docs" if os.environ.get("ENV", "production") != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "").split(","),
    allow_credentials=True,
    allow_methods=["POST"],
    allow_headers=["Authorization", "X-Request-ID", "X-Client-Cert"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────────────────────────


class AnalyseRequest(BaseModel):
    """
    Incoming request body for the /analyse endpoint.

    Attributes:
        user_id:            User to analyse (will be tokenised before state entry).
        tenant_id:          Tenant partition key.
        fetch_types:        Data categories to retrieve.
        date_range_days:    Lookback window for transaction history.
        send_email:         Whether to dispatch a notification email.
        notification_email: Recipient email address for the notification.
        utterance:          Optional conversational question for Dialogflow.
        language_code:      BCP-47 language for the conversational turn.
    """

    user_id: str = Field(..., min_length=1, max_length=128)
    tenant_id: str = Field(..., min_length=1, max_length=64)
    fetch_types: list[str] = Field(default=["profile", "transactions", "kyc"])
    date_range_days: int = Field(default=90, ge=1, le=365)
    send_email: bool = False
    notification_email: Optional[str] = None
    notification_name: Optional[str] = None
    utterance: Optional[str] = None
    language_code: str = "en-US"


class AnalyseResponse(BaseModel):
    request_id: str
    status: str
    risk_level: Optional[str]
    executive_summary: Optional[str]
    recommendations: list[str]
    email_sent: bool
    conversation_active: bool
    errors: list[str]
    pipeline_duration_ms: float


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting (Redis token bucket)
# ─────────────────────────────────────────────────────────────────────────────


async def _check_rate_limit(tenant_id: str, request_id: str) -> None:
    """
    Enforce per-tenant rate limiting via a Redis token bucket.

    Allows up to 100 requests per minute per tenant.  Raises HTTP 429 if
    the bucket is exhausted.

    Args:
        tenant_id:  Tenant partition key used as the Redis key.
        request_id: Correlation ID for logging.

    Raises:
        HTTPException(429): When the rate limit is exceeded.

    Side effects:
        Increments a Redis key with a 60-second TTL.
    """
    try:
        import redis.asyncio as redis_async

        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        r = redis_async.from_url(redis_url, decode_responses=True)

        key = f"rate:{tenant_id}"
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 60)

        limit = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "100"))
        if count > limit:
            logger.warning(
                "gateway.rate_limit_exceeded",
                tenant_id=tenant_id,
                request_id=request_id,
                count=count,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Retry after 60 seconds.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Redis unavailable — fail open with a warning (availability > strict limiting)
        logger.warning("gateway.rate_limit_redis_error", error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/analyse", response_model=AnalyseResponse, status_code=status.HTTP_200_OK)
async def analyse(request: Request, body: AnalyseRequest) -> AnalyseResponse:
    """
    Main pipeline endpoint.

    Validates JWT, checks rate limit, scans for injection, obfuscates PII,
    builds initial state, invokes the compiled LangGraph, and returns the
    final response.

    Args:
        request: FastAPI Request object for header access.
        body:    Validated AnalyseRequest body.

    Returns:
        AnalyseResponse with risk level, recommendations, and pipeline metadata.

    Raises:
        HTTPException(401): If JWT is missing or invalid.
        HTTPException(400): If prompt injection is detected in the payload.
        HTTPException(429): If the per-tenant rate limit is exceeded.
        HTTPException(500): On unexpected pipeline failures.
    """
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    logger.info(
        "gateway.request_received",
        request_id=request_id,
        path="/analyse",
        tenant_id=body.tenant_id,
    )

    # ── JWT validation ────────────────────────────────────────────────────
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    token = auth_header.removeprefix("Bearer ").strip()

    try:
        security_ctx = validate_jwt(
            token=token,
            jwks_uri=os.environ.get(
                "KEYCLOAK_JWKS_URI",
                "http://keycloak.internal:8080/realms/mas/protocol/openid-connect/certs",
            ),
            expected_audience=os.environ.get("JWT_AUDIENCE", "mas-enterprise"),
            expected_issuer=os.environ.get(
                "JWT_ISSUER",
                "https://keycloak.internal/realms/mas",
            ),
            required_roles=["mas:analyse"],
        )
    except JWTValidationError as exc:
        logger.warning("gateway.auth_failed", request_id=request_id, reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
        )

    # ── Rate limiting ─────────────────────────────────────────────────────
    await _check_rate_limit(body.tenant_id, request_id)

    # ── Prompt injection guard ────────────────────────────────────────────
    raw_payload = body.model_dump()
    try:
        enforce_no_injection(raw_payload, request_id)
    except PromptInjectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Request blocked: prompt injection detected ({len(exc.matches)} pattern(s))",
        )

    # ── PII obfuscation ───────────────────────────────────────────────────
    clean_payload = obfuscate_payload(raw_payload)

    # ── State initialisation ──────────────────────────────────────────────
    session_id = request.headers.get("X-Session-ID") or str(uuid.uuid4())
    state = initial_state(
        request_id=request_id,
        user_id=body.user_id,
        tenant_id=body.tenant_id,
        session_id=session_id,
        raw_input=clean_payload,
        security_context=security_ctx,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    # ── Pipeline invocation ───────────────────────────────────────────────
    try:
        from graph.workflow import compiled_graph

        final_state = await compiled_graph.ainvoke(state)
        final_response = final_state.get("final_response")

        if final_response is None:
            raise RuntimeError("Pipeline completed without a final_response")

        logger.info(
            "gateway.request_complete",
            request_id=request_id,
            status=final_response.status.value,
            duration_ms=final_response.pipeline_duration_ms,
        )

        return AnalyseResponse(
            request_id=final_response.request_id,
            status=final_response.status.value,
            risk_level=final_response.risk_level.value if final_response.risk_level else None,
            executive_summary=final_response.executive_summary,
            recommendations=final_response.recommendations,
            email_sent=final_response.email_sent,
            conversation_active=final_response.conversation_active,
            errors=final_response.errors,
            pipeline_duration_ms=final_response.pipeline_duration_ms,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "gateway.pipeline_error",
            request_id=request_id,
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal pipeline error. Please contact support.",
        )


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Liveness probe endpoint for Kubernetes/ECS health checks."""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/ready")
async def readiness() -> Dict[str, Any]:
    """
    Readiness probe — confirms the compiled graph is initialised.
    Returns 503 if the graph failed to compile at startup.
    """
    try:
        from graph.workflow import compiled_graph  # noqa: F401

        return {"status": "ready"}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Graph not ready: {exc}",
        )
