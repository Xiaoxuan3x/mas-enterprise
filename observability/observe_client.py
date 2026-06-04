"""
Observe integration client for pipeline observability.

Observe (https://observeinc.com) ingests structured JSON events via its
HTTP ingest endpoint.  This client ships every agent transition, token usage,
and final response as a separate observation to an Observe datastream.

All submissions are fire-and-forget with a short timeout — observability
failures never block the pipeline (best-effort telemetry).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from core.logging_config import get_logger
from schemas.agent_io import AgentExecution, FinalResponse, TokenUsage

logger = get_logger(__name__)


class ObserveClient:
    """
    HTTP client that forwards structured events to an Observe datastream.

    Attributes:
        customer_id:  Observe customer identifier.
        ingest_token: Datastream ingest token.
        datastream:   Target datastream slug.
        base_url:     Observe ingest base URL.
        timeout_s:    HTTP request timeout in seconds.
    """

    def __init__(
        self,
        customer_id: Optional[str] = None,
        ingest_token: Optional[str] = None,
        datastream: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_s: float = 3.0,
    ) -> None:
        self.customer_id = customer_id or os.environ.get("OBSERVE_CUSTOMER_ID", "")
        self.ingest_token = ingest_token or os.environ.get("OBSERVE_INGEST_TOKEN", "")
        self.datastream = datastream or os.environ.get("OBSERVE_DATASTREAM", "mas-enterprise")
        self.base_url = base_url or os.environ.get(
            "OBSERVE_BASE_URL", "https://collect.observeinc.com"
        )
        self.timeout_s = timeout_s

    @property
    def _ingest_url(self) -> str:
        return f"{self.base_url}/v1/http/{self.customer_id}/{self.datastream}"

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.ingest_token}",
            "Content-Type": "application/json",
        }

    def _is_configured(self) -> bool:
        return bool(self.customer_id and self.ingest_token)

    def emit_agent_execution(
        self,
        execution: AgentExecution,
        request_id: str,
        tenant_id: str,
    ) -> None:
        """
        Ship an AgentExecution record to Observe as a structured event.

        Args:
            execution:  The AgentExecution record from state's execution_history.
            request_id: End-to-end correlation ID.
            tenant_id:  Tenant partition key for multi-tenant filtering.

        Side effects:
            Sends an async-like fire-and-forget HTTP POST.
            Logs a warning on failure but does not raise.
        """
        if not self._is_configured():
            return

        event = {
            "event_type": "agent_execution",
            "request_id": request_id,
            "tenant_id": tenant_id,
            "agent_name": execution.agent_name,
            "status": execution.status.value,
            "platform": execution.platform,
            "duration_ms": execution.duration_ms,
            "retry_attempt": execution.retry_attempt,
            "started_at": execution.started_at.isoformat(),
            "finished_at": execution.finished_at.isoformat(),
            "input_summary": execution.input_summary,
            "output_summary": execution.output_summary,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._post(event)

    def emit_token_usage(
        self,
        agent_name: str,
        usage: TokenUsage,
        request_id: str,
        tenant_id: str,
    ) -> None:
        """
        Ship an LLM token-usage record to Observe for cost tracking.

        Args:
            agent_name: Name of the LLM-backed agent.
            usage:      TokenUsage model with prompt/completion/total counts.
            request_id: Correlation ID.
            tenant_id:  Tenant key.
        """
        if not self._is_configured():
            return

        event = {
            "event_type": "token_usage",
            "request_id": request_id,
            "tenant_id": tenant_id,
            "agent_name": agent_name,
            "model_id": usage.model_id,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": usage.cost_usd,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._post(event)

    def emit_pipeline_result(
        self,
        final_response: FinalResponse,
        tenant_id: str,
    ) -> None:
        """
        Ship the final pipeline result to Observe for dashboard analytics.

        Args:
            final_response: Aggregated FinalResponse from the finalize node.
            tenant_id:      Tenant key.
        """
        if not self._is_configured():
            return

        event = {
            "event_type": "pipeline_result",
            "request_id": final_response.request_id,
            "tenant_id": tenant_id,
            "user_id": final_response.user_id,
            "status": final_response.status.value,
            "risk_level": final_response.risk_level.value if final_response.risk_level else None,
            "email_sent": final_response.email_sent,
            "pipeline_duration_ms": final_response.pipeline_duration_ms,
            "error_count": len(final_response.errors),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._post(event)

    def _post(self, event: Dict[str, Any]) -> None:
        """
        Synchronous fire-and-forget HTTP POST to the Observe ingest endpoint.

        Uses a short timeout so observability never blocks the pipeline.
        Failures are logged but not re-raised.

        Args:
            event: Dict that will be serialised to JSON and shipped.
        """
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                response = client.post(
                    self._ingest_url,
                    json=event,
                    headers=self._headers,
                )
                if response.status_code >= 400:
                    logger.warning(
                        "observe.ingest_error",
                        status_code=response.status_code,
                        body=response.text[:200],
                    )
        except Exception as exc:
            logger.warning("observe.client_error", error=str(exc))


# Module-level singleton — avoids re-instantiating the client per request
observe = ObserveClient()
