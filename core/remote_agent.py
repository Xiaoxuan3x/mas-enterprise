"""
Remote invocation helpers for distributed MAS agent services.

The orchestrator uses these helpers to call agents running as independent HTTP
services while preserving the existing MASState patch semantics.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from core.guardrails import build_fallback_supervisor_output
from core.logging_config import get_logger
from core.security import sign_request
from schemas.agent_io import AgentError, AgentExecution, AgentStatus
from schemas.distributed_io import AgentServiceRequest, AgentServiceResponse, StateSnapshot

logger = get_logger(__name__)


@dataclass(frozen=True)
class RemoteAgentConfig:
    """Runtime configuration for a single remote MAS agent service."""

    agent_name: str
    platform: str
    service_url: str
    timeout_seconds: float = 60.0
    max_attempts: int = 3


class RemoteAgentInvocationError(Exception):
    """Raised when a remote agent service cannot be reached successfully."""


def _get_hmac_secret() -> bytes | None:
    """Return the shared HMAC key used for inter-service request signing."""
    secret = os.environ.get("INTER_SERVICE_HMAC_KEY", "").strip()
    return secret.encode() if secret else None


def _build_failure_patch(
    *,
    config: RemoteAgentConfig,
    state: Dict[str, Any],
    error_message: str,
    duration_ms: float,
) -> Dict[str, Any]:
    """
    Build a failure patch when a remote service is unreachable.

    The patch mirrors the structure returned by the local agents so the graph
    can continue to route consistently even on transport-level failures.
    """
    started_at = datetime.now(timezone.utc)
    patch: Dict[str, Any] = {
        "current_agent": config.agent_name,
        "execution_history": [
            AgentExecution(
                agent_name=config.agent_name,
                status=AgentStatus.FAILURE,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                duration_ms=round(duration_ms, 2),
                output_summary={"transport_error": error_message[:200]},
                platform=config.platform,
            )
        ],
        "errors": [
            AgentError(
                agent_name=config.agent_name,
                error_type="RemoteAgentInvocationError",
                message=error_message,
                is_fatal=config.agent_name in {"data_fetcher", "data_validator"},
            )
        ],
        "execution_times": {config.agent_name: round(duration_ms / 1000, 4)},
    }

    if config.agent_name == "supervisor":
        patch["supervisor_output"] = build_fallback_supervisor_output(
            user_id=state["user_id"],
            model_id=os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-pro"),
            error_detail=error_message,
        )
        patch["token_usage"] = {}

    return patch


class RemoteAgentInvoker:
    """
    HTTP client wrapper that calls one remote agent service and returns a patch.

    Transport failures are retried with exponential backoff. If the remote
    service still cannot be reached, a structured failure patch is returned so
    the orchestrator can continue with its normal fallback/error routing.
    """

    def __init__(
        self,
        config: RemoteAgentConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute one remote agent call and return the validated state patch.

        Args:
            state: Current orchestrator-owned MAS state dict.

        Returns:
            State patch from the remote service or a synthetic failure patch.
        """
        payload = AgentServiceRequest(state=StateSnapshot.from_state(state))
        body_json = payload.model_dump(mode="json")
        body_bytes = json.dumps(body_json, separators=(",", ":"), sort_keys=True).encode()
        headers = {"Content-Type": "application/json", "X-Request-ID": state["request_id"]}

        secret = _get_hmac_secret()
        if secret is not None:
            headers["X-MAS-Signature"] = sign_request(body_bytes, secret)

        start_time = time.perf_counter()

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.config.max_attempts),
                wait=wait_exponential_jitter(initial=1, max=30, jitter=1),
                retry=retry_if_exception_type((httpx.HTTPError, RemoteAgentInvocationError)),
                reraise=True,
            ):
                with attempt:
                    async with httpx.AsyncClient(
                        base_url=self.config.service_url,
                        timeout=self.config.timeout_seconds,
                        transport=self.transport,
                    ) as client:
                        response = await client.post("run", content=body_bytes, headers=headers)
                        response.raise_for_status()
                        parsed = AgentServiceResponse.model_validate(response.json())
                        return parsed.state_patch.to_patch()

        except (httpx.HTTPError, RemoteAgentInvocationError) as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "remote_agent.invoke_failed",
                agent_name=self.config.agent_name,
                service_url=self.config.service_url,
                request_id=state["request_id"],
                error=str(exc),
            )
            return _build_failure_patch(
                config=self.config,
                state=state,
                error_message=str(exc),
                duration_ms=duration_ms,
            )
