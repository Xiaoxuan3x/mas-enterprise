"""
Policy evaluation gate nodes for the LangGraph workflow.

These nodes evaluate declarative Control Tower policies at key points in the
pipeline so deny rules can stop execution before downstream side effects, while
audit-required rules are recorded in state metadata for later inspection.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

from control_tower.policy_engine import PolicyViolation, policy_engine
from core.logging_config import agent_span, get_logger
from schemas.agent_io import AgentError, AgentExecution, AgentStatus
from schemas.state import MASState

logger = get_logger(__name__)

AGENT_NAME = "policy_gate"
PLATFORM = "control-tower"


def _build_context(state: MASState) -> Dict[str, Any]:
    """Build the policy context from request, fetched, and analysed state."""
    raw_input = state.get("raw_input", {})
    context: Dict[str, Any] = {
        "processing_region": os.environ.get("AWS_REGION", "us-east-1"),
        "send_email": raw_input.get("send_email", False),
        "language_code": raw_input.get("language_code", "en-US"),
        "fetch_types": raw_input.get("fetch_types", []),
    }

    fetched = state.get("fetched_data")
    if fetched is not None:
        context["country_of_residence"] = fetched.profile.country_of_residence
        context["kyc_status"] = fetched.profile.kyc_status

    analysis = state.get("analysis_result")
    if analysis is not None:
        context["risk_level"] = analysis.risk_level.value

    validation = state.get("validation_result")
    if validation is not None:
        context["validation_issue_count"] = len(validation.issues)
        context["validation_critical_issue_count"] = validation.critical_issue_count

    return context


def _merge_metadata(state: MASState, **updates: Any) -> Dict[str, Any]:
    metadata = dict(state.get("metadata", {}))
    metadata.update(updates)
    return metadata


async def _evaluate(state: MASState, stage: str) -> Dict[str, Any]:
    """Evaluate policies for the current state and return a LangGraph patch."""
    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    node_name = f"{AGENT_NAME}_{stage}"

    with agent_span(logger, node_name, state["request_id"]) as span:
        if not getattr(policy_engine, "_policies", []):
            policy_engine.load_policies()

        context = _build_context(state)
        try:
            result = policy_engine.evaluate(
                operation="analyse",
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                context=context,
            )

            duration_ms = (time.perf_counter() - start_time) * 1000
            span["decision"] = result.decision.value
            span["policy_name"] = result.policy_name

            audit_stages = list(state.get("metadata", {}).get("policy_audit_stages", []))
            if result.requires_audit and stage not in audit_stages:
                audit_stages.append(stage)

            return {
                "current_agent": node_name,
                "metadata": _merge_metadata(
                    state,
                    policy_denied=False,
                    policy_stage=stage,
                    policy_name=result.policy_name,
                    policy_reason=result.reason,
                    policy_requires_audit=result.requires_audit
                    or state.get("metadata", {}).get("policy_requires_audit", False),
                    policy_audit_stages=audit_stages,
                ),
                "execution_history": [
                    AgentExecution(
                        agent_name=node_name,
                        status=AgentStatus.SUCCESS,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        output_summary={
                            "decision": result.decision.value,
                            "policy_name": result.policy_name,
                        },
                        platform=PLATFORM,
                    )
                ],
                "errors": [],
                "execution_times": {node_name: round(duration_ms / 1000, 4)},
            }

        except PolicyViolation as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            return {
                "current_agent": node_name,
                "metadata": _merge_metadata(
                    state,
                    policy_denied=True,
                    policy_stage=stage,
                    policy_name=exc.policy_name,
                    policy_reason=exc.reason,
                    policy_requires_audit=state.get("metadata", {}).get(
                        "policy_requires_audit", False
                    ),
                ),
                "execution_history": [
                    AgentExecution(
                        agent_name=node_name,
                        status=AgentStatus.FAILURE,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        output_summary={"policy_name": exc.policy_name},
                        platform=PLATFORM,
                    )
                ],
                "errors": [
                    AgentError(
                        agent_name=node_name,
                        error_type="PolicyViolation",
                        message=exc.reason,
                        is_fatal=True,
                    )
                ],
                "execution_times": {node_name: round(duration_ms / 1000, 4)},
            }


async def pre_analysis_run(state: MASState) -> Dict[str, Any]:
    """Evaluate request and fetched-data policies before analysis."""
    return await _evaluate(state, stage="pre_analysis")


async def post_analysis_run(state: MASState) -> Dict[str, Any]:
    """Evaluate analysis-dependent policies before supervisor/email actions."""
    return await _evaluate(state, stage="post_analysis")
