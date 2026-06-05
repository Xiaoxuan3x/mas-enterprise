"""
FastAPI runtime for remotely deployed MAS agents.

Each service exposes a small `/run` endpoint that accepts a validated state
snapshot from the orchestrator, executes exactly one agent, and returns a
validated state patch.
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Dict

from fastapi import FastAPI, Header, HTTPException, Request, status

from core.logging_config import get_logger
from core.security import verify_request_signature
from schemas.distributed_io import AgentServiceRequest, AgentServiceResponse, StatePatch

logger = get_logger(__name__)

AgentRunner = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _get_hmac_secret() -> bytes | None:
    """
    Return the shared inter-service HMAC key, if configured.

    The deployment should source this from Vault or a cloud secret store and
    inject it as an environment variable into each service container.
    """
    secret = os.environ.get("INTER_SERVICE_HMAC_KEY", "").strip()
    return secret.encode() if secret else None


def create_agent_service_app(
    *,
    agent_name: str,
    platform: str,
    runner: AgentRunner,
) -> FastAPI:
    """
    Build a standalone FastAPI application for a single MAS agent.

    Args:
        agent_name: Logical agent identifier used in logs and responses.
        platform:   Deployment platform label such as `aws` or `gcp`.
        runner:     Async callable implementing the agent's state patch logic.

    Returns:
        FastAPI application exposing `/run`, `/health`, and `/ready`.
    """
    app = FastAPI(
        title=f"MAS Agent Service: {agent_name}",
        version="1.0.0",
        docs_url="/docs" if os.environ.get("ENV", "production") != "production" else None,
        redoc_url=None,
    )

    @app.post("/run", response_model=AgentServiceResponse, status_code=status.HTTP_200_OK)
    async def run_agent(
        payload: AgentServiceRequest,
        request: Request,
        x_mas_signature: str | None = Header(default=None),
    ) -> AgentServiceResponse:
        """
        Execute one remote agent invocation on behalf of the orchestrator.

        The request body is optionally protected by an HMAC signature to
        enforce a zero-trust internal call pattern.
        """
        secret = _get_hmac_secret()
        raw_body = await request.body()
        if secret is not None:
            if not x_mas_signature:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Missing X-MAS-Signature header",
                )
            if not verify_request_signature(raw_body, x_mas_signature, secret):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid X-MAS-Signature header",
                )

        patch = await runner(payload.state.to_state())
        state_patch = StatePatch.model_validate(patch)

        logger.info(
            "agent_service.run_complete",
            agent_name=agent_name,
            platform=platform,
            request_id=payload.state.request_id,
        )
        return AgentServiceResponse(agent_name=agent_name, state_patch=state_patch)

    @app.get("/health")
    async def health() -> Dict[str, str]:
        """Liveness probe for container and load-balancer health checks."""
        return {"status": "healthy", "agent": agent_name, "platform": platform}

    @app.get("/ready")
    async def ready() -> Dict[str, str]:
        """Readiness probe used before routing production traffic to the service."""
        return {"status": "ready", "agent": agent_name}

    return app
