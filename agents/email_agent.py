"""
Email Agent / Copilot — Platform: Azure Communication Services

Sends structured risk-assessment notification emails via Azure Communication
Services.  The email content is generated from the SupervisorOutput and
AnalysisResult using a Jinja2-style template (no LLM involved in rendering).

Inputs:  SupervisorOutput + AnalysisResult from state.
Outputs: EmailDeliveryReceipt.
Platform: Azure (azure-communication-email SDK).
Retry:   Up to 3 attempts with exponential backoff on transient Azure errors.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from schemas.agent_io import (
    AgentError,
    AgentExecution,
    AgentStatus,
    AnalysisResult,
    EmailDeliveryReceipt,
    EmailPayload,
    EmailPriority,
    RiskLevel,
    SupervisorOutput,
)
from schemas.state import MASState
from core.logging_config import agent_span, get_logger

logger = get_logger(__name__)

AGENT_NAME = "email_agent"
PLATFORM = "azure"


# ─────────────────────────────────────────────────────────────────────────────
# Email template rendering
# ─────────────────────────────────────────────────────────────────────────────


def _render_html(
    supervisor_output: SupervisorOutput,
    analysis: AnalysisResult,
    recipient_name: str,
) -> str:
    """
    Render an HTML email body from the risk analysis results.

    Uses f-string templates (no external template engine dependency).

    Args:
        supervisor_output: Gemini-generated summary and recommendations.
        analysis:          Deterministic risk score and signals.
        recipient_name:    Display name for the salutation.

    Returns:
        HTML string safe for sending via Azure Communication Services.
    """
    risk_colour = {
        RiskLevel.LOW: "#27ae60",
        RiskLevel.MEDIUM: "#f39c12",
        RiskLevel.HIGH: "#e67e22",
        RiskLevel.CRITICAL: "#c0392b",
    }.get(analysis.risk_level, "#7f8c8d")

    recommendation_rows = "".join(
        f"<tr><td>{r.priority}</td><td>{r.action}</td>"
        f"<td>{r.owner}</td><td>{r.timeline}</td></tr>"
        for r in supervisor_output.strategic_recommendations
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Risk Assessment Report</title></head>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;">
  <h2 style="color:#2c3e50;">Risk Assessment Notification</h2>
  <p>Dear {recipient_name},</p>
  <p>An automated risk assessment has been completed for account
     <strong>{analysis.user_id}</strong>.</p>

  <div style="background:{risk_colour};color:#fff;padding:12px;border-radius:6px;">
    <strong>Risk Level: {analysis.risk_level.value.upper()}</strong>
    &nbsp;|&nbsp; Score: {analysis.composite_risk_score:.1f}/100
  </div>

  <h3>Executive Summary</h3>
  <p>{supervisor_output.executive_summary}</p>

  <h3>Strategic Recommendations</h3>
  <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
    <thead style="background:#ecf0f1;">
      <tr><th>Priority</th><th>Action</th><th>Owner</th><th>Timeline</th></tr>
    </thead>
    <tbody>{recommendation_rows}</tbody>
  </table>

  <h3>Risk Narrative</h3>
  <p>{supervisor_output.risk_narrative}</p>

  <p style="color:#7f8c8d;font-size:12px;">
    This report was generated automatically by the MAS Enterprise risk platform.
    Confidence score: {supervisor_output.confidence_score:.0%}.
    Generated at: {supervisor_output.generated_at.isoformat()}.
  </p>
</body>
</html>"""


def _render_plain_text(
    supervisor_output: SupervisorOutput,
    analysis: AnalysisResult,
) -> str:
    """
    Render a plain-text fallback email body.

    Args:
        supervisor_output: Gemini-generated supervisor output.
        analysis:          Deterministic analysis result.

    Returns:
        Plain text string for the email alternative part.
    """
    recs = "\n".join(
        f"  {r.priority}. [{r.owner} / {r.timeline}] {r.action}"
        for r in supervisor_output.strategic_recommendations
    )
    return (
        f"RISK ASSESSMENT NOTIFICATION\n"
        f"{'='*40}\n"
        f"User: {analysis.user_id}\n"
        f"Risk Level: {analysis.risk_level.value.upper()} "
        f"(Score: {analysis.composite_risk_score:.1f}/100)\n\n"
        f"SUMMARY:\n{supervisor_output.executive_summary}\n\n"
        f"RECOMMENDATIONS:\n{recs}\n\n"
        f"Generated: {supervisor_output.generated_at.isoformat()}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Azure Communication Services sender
# ─────────────────────────────────────────────────────────────────────────────


def _send_via_azure(payload: EmailPayload, connection_string: str) -> EmailDeliveryReceipt:
    """
    Send an email using Azure Communication Services.

    Args:
        payload:           Validated EmailPayload model.
        connection_string: Azure Communication Services connection string.

    Returns:
        EmailDeliveryReceipt with Azure message ID and status.

    Raises:
        Exception: On Azure SDK errors (handled by caller for retry).
    """
    from azure.communication.email import EmailClient

    client = EmailClient.from_connection_string(connection_string)

    sender_address = os.environ.get(
        "AZURE_EMAIL_SENDER", "DoNotReply@notifications.mas-enterprise.com"
    )

    message = {
        "senderAddress": sender_address,
        "recipients": {
            "to": [
                {
                    "address": payload.recipient_email,
                    "displayName": payload.recipient_name,
                }
            ]
        },
        "content": {
            "subject": payload.subject,
            "html": payload.html_body,
            "plainText": payload.plain_text_body,
        },
        "importance": payload.priority.value,
    }

    poller = client.begin_send(message)
    result = poller.result()

    return EmailDeliveryReceipt(
        message_id=result.get("id", "unknown"),
        recipient_email=payload.recipient_email,
        status=result.get("status", "Submitted"),
        provider="azure-communication-services",
    )


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node
# ─────────────────────────────────────────────────────────────────────────────


async def run(state: MASState) -> Dict[str, Any]:
    """
    LangGraph node function for the Email Agent.

    Builds an email payload from SupervisorOutput and AnalysisResult, then
    dispatches via Azure Communication Services with up to 3 retries.

    Args:
        state: Current MASState containing ``supervisor_output`` and
               ``analysis_result``.

    Returns:
        Partial MASState dict with ``email_receipt``, ``execution_history``,
        ``errors``, and ``execution_times``.
    """
    from tenacity import (
        AsyncRetrying,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )

    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc)

    with agent_span(logger, AGENT_NAME, state["request_id"]) as span:
        try:
            supervisor: Optional[SupervisorOutput] = state.get("supervisor_output")
            analysis: Optional[AnalysisResult] = state.get("analysis_result")

            if supervisor is None or analysis is None:
                raise ValueError(
                    "Email agent requires supervisor_output and analysis_result"
                )

            recipient_email = state["raw_input"].get(
                "notification_email",
                state["metadata"].get("user_email", "compliance@mas-enterprise.com"),
            )
            recipient_name = state["raw_input"].get("notification_name", "Compliance Team")

            priority = (
                EmailPriority.HIGH
                if analysis.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
                else EmailPriority.NORMAL
            )

            payload = EmailPayload(
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                subject=f"[{analysis.risk_level.value.upper()}] Risk Assessment — User {analysis.user_id[:8]}***",
                html_body=_render_html(supervisor, analysis, recipient_name),
                plain_text_body=_render_plain_text(supervisor, analysis),
                priority=priority,
            )

            connection_string = os.environ.get("AZURE_COMMUNICATION_CONNECTION_STRING", "")
            if not connection_string:
                raise RuntimeError("AZURE_COMMUNICATION_CONNECTION_STRING is not set")

            receipt: Optional[EmailDeliveryReceipt] = None
            attempt = 0

            async for attempt_obj in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential_jitter(initial=1, max=30, jitter=1),
                retry=retry_if_exception_type(Exception),
                reraise=True,
            ):
                with attempt_obj:
                    attempt += 1
                    if attempt > 1:
                        logger.warning("agent.retry", agent=AGENT_NAME, attempt=attempt)
                    receipt = _send_via_azure(payload, connection_string)

            duration_ms = (time.perf_counter() - start_time) * 1000
            span["message_id"] = receipt.message_id if receipt else "none"
            span["recipient"] = recipient_email[:20] + "***"

            return {
                "email_receipt": receipt,
                "current_agent": AGENT_NAME,
                "execution_history": [
                    AgentExecution(
                        agent_name=AGENT_NAME,
                        status=AgentStatus.SUCCESS,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        output_summary={"message_id": receipt.message_id if receipt else ""},
                        platform=PLATFORM,
                    )
                ],
                "errors": [],
                "execution_times": {AGENT_NAME: round(duration_ms / 1000, 4)},
            }

        except Exception as exc:
            import traceback

            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("email_agent.send_failed", error=str(exc))

            return {
                "email_receipt": EmailDeliveryReceipt(
                    message_id="FAILED",
                    recipient_email="unknown",
                    status="Failed",
                    error_detail=str(exc)[:200],
                ),
                "current_agent": AGENT_NAME,
                "execution_history": [
                    AgentExecution(
                        agent_name=AGENT_NAME,
                        status=AgentStatus.FAILURE,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        platform=PLATFORM,
                    )
                ],
                "errors": [
                    AgentError(
                        agent_name=AGENT_NAME,
                        error_type=type(exc).__name__,
                        message=str(exc),
                        traceback=traceback.format_exc(),
                        is_fatal=False,
                    )
                ],
                "execution_times": {AGENT_NAME: round(duration_ms / 1000, 4)},
            }
