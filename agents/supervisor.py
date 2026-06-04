"""
Supervisor Agent (Non-Deterministic Agent C) — Platform: On-Prem (NVIDIA GPU)

LLM-backed supervisor using Google Gemini (served on-prem via NVIDIA AI
Enterprise or accessed via Vertex AI on-prem endpoint).

Responsibilities:
  - Aggregates validated data + analysis results into a natural language summary.
  - Generates strategic recommendations with priorities and owners.
  - Handles agent failures gracefully (fallback degradation response).
  - Manages state and memory for multi-turn conversational context.
  - Validates its own output through the guardrail layer before returning.

Model: gemini-2.5-pro (configurable via GEMINI_MODEL_ID env var)
Platform: On-prem via vLLM/NVIDIA NIM endpoint or Vertex AI on-prem.

Inputs:  SupervisorInput (assembled by orchestrator from fetched+validated+analysis).
Outputs: SupervisorOutput (executive_summary, recommendations, risk_narrative).
Failure: Catches API errors, schema failures, guardrail rejections and activates
         build_fallback_supervisor_output() for graceful degradation.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from schemas.agent_io import (
    AgentError,
    AgentExecution,
    AgentStatus,
    AnalysisResult,
    FetchedData,
    SupervisorInput,
    SupervisorOutput,
    TokenUsage,
    ValidationResult,
)
from schemas.state import MASState
from core.guardrails import GuardrailViolation, build_fallback_supervisor_output, run_guardrails
from core.logging_config import agent_span, get_logger, log_token_usage

logger = get_logger(__name__)

AGENT_NAME = "supervisor"
PLATFORM = "on-prem"
DEFAULT_MODEL_ID = "gemini-2.5-pro"

# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an enterprise fraud-risk supervisor AI running on a
secure on-premises system. You receive structured risk analysis data and must
produce a concise, actionable executive summary for the compliance team.

You MUST respond with a valid JSON object matching exactly this schema:
{
  "user_id": "<string>",
  "executive_summary": "<string, 50-500 chars>",
  "strategic_recommendations": [
    {
      "priority": <1-5 int, 1=highest>,
      "action": "<string>",
      "rationale": "<string>",
      "owner": "<string>",
      "timeline": "<string>"
    }
  ],
  "risk_narrative": "<string>",
  "next_steps": ["<string>"],
  "confidence_score": <float 0.0-1.0>,
  "model_id": "<string>"
}

Rules:
- Do NOT include any PII (card numbers, SSNs, raw email addresses).
- Do NOT include internal system prompts or instruction artefacts.
- Provide between 1 and 5 strategic recommendations.
- confidence_score reflects your certainty in the assessment (0=uncertain, 1=certain).
"""


def _build_user_prompt(supervisor_input: SupervisorInput, model_id: str) -> str:
    """
    Construct the user-turn prompt from structured analysis data.

    Args:
        supervisor_input: Aggregated data from orchestrator.
        model_id:         Model ID to embed in the response schema.

    Returns:
        A formatted prompt string for the Gemini model.
    """
    analysis = supervisor_input.analysis_result
    validation = supervisor_input.validation_result
    profile = supervisor_input.fetched_data.profile

    if analysis is not None:
        risk_analysis_block = f"""RISK ANALYSIS:
- Composite risk score: {analysis.composite_risk_score:.1f}/100
- Risk level: {analysis.risk_level.value.upper()}
- Fraud signals: {", ".join(f"{s.signal_name}={s.score:.2f}" for s in analysis.fraud_signals)}
- Recommended action: {analysis.recommended_action}
- Requires human review: {analysis.requires_human_review}
- Analyst explanation: {analysis.explanation}
"""
    else:
        risk_analysis_block = """RISK ANALYSIS:
- Automated scoring was skipped because the upstream data failed critical validation.
- Produce a degraded summary focused on data quality issues and required manual review.
- Do not infer a numeric risk score or precise risk level from incomplete data.
"""

    return f"""Analyse the following risk assessment and produce the required JSON response.

USER CONTEXT:
- User ID: {supervisor_input.user_id}
- Tenant: {supervisor_input.tenant_id}
- KYC Status: {profile.kyc_status}
- Account age: {profile.account_age_days} days
- Country: {profile.country_of_residence}

{risk_analysis_block}

VALIDATION:
- Data valid: {validation.is_valid}
- Critical issues: {validation.critical_issue_count}
- Total issues: {len(validation.issues)}

TRANSACTION STATS:
- Transaction count: {validation.transaction_count}

Respond with the JSON object only. No markdown. No additional text.
Set model_id to "{model_id}".
"""


# ─────────────────────────────────────────────────────────────────────────────
# Gemini client
# ─────────────────────────────────────────────────────────────────────────────


def _invoke_gemini(
    system_prompt: str,
    user_prompt: str,
    model_id: str,
    on_prem_endpoint: Optional[str],
    max_output_tokens: int = 1024,
    temperature: float = 0.2,
) -> tuple[str, TokenUsage]:
    """
    Invoke the Gemini model via Vertex AI SDK.

    When ``on_prem_endpoint`` is set, the Vertex AI client is configured to
    route requests to the on-prem NVIDIA AI Enterprise inference endpoint
    instead of Google Cloud.

    Args:
        system_prompt:      System-level instruction for the model.
        user_prompt:        User-turn prompt with structured data.
        model_id:           Gemini model identifier.
        on_prem_endpoint:   Optional on-prem API base URL.
        max_output_tokens:  Token budget for the completion.
        temperature:        Sampling temperature (lower = more deterministic).

    Returns:
        Tuple of (raw_text_response, TokenUsage).

    Raises:
        RuntimeError: On API error, timeout, or empty response.
    """
    try:
        import google.generativeai as genai
        from google.generativeai.types import HarmBlockThreshold, HarmCategory

        api_key = os.environ.get("GEMINI_API_KEY")
        if on_prem_endpoint:
            # Point SDK at on-prem NVIDIA NIM endpoint running Gemini-compatible API
            genai.configure(api_key=api_key or "on-prem", transport="rest")
            # For on-prem, override the base URL via environment
            os.environ.setdefault(
                "GOOGLE_API_USE_MTLS_ENDPOINT", on_prem_endpoint
            )
        elif api_key:
            genai.configure(api_key=api_key)
        else:
            raise RuntimeError(
                "Neither GEMINI_API_KEY nor GEMINI_ON_PREM_ENDPOINT is configured"
            )

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        generation_config = genai.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
        )

        model = genai.GenerativeModel(
            model_name=model_id,
            system_instruction=system_prompt,
            generation_config=generation_config,
            safety_settings=safety_settings,
        )

        response = model.generate_content(user_prompt)

        if not response.candidates:
            raise RuntimeError("Gemini returned no candidates")

        raw_text = response.text.strip()

        usage_meta = getattr(response, "usage_metadata", None)
        token_usage = TokenUsage(
            prompt_tokens=getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0,
            completion_tokens=getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0,
            total_tokens=getattr(usage_meta, "total_token_count", 0) if usage_meta else 0,
            model_id=model_id,
        )

        return raw_text, token_usage

    except ImportError:
        raise RuntimeError(
            "google-generativeai package is not installed. "
            "Run: pip install google-generativeai"
        )


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node
# ─────────────────────────────────────────────────────────────────────────────


async def run(state: MASState) -> Dict[str, Any]:
    """
    LangGraph node function for the Supervisor agent.

    Builds the supervisor prompt from state, calls Gemini, parses the JSON
    response, runs guardrails, and returns the validated SupervisorOutput.
    On any failure (API error, parse error, guardrail violation), activates
    the graceful degradation fallback and logs the incident.

    Args:
        state: Current MASState containing ``fetched_data``,
               ``validation_result``, and ``analysis_result``.

    Returns:
        Partial MASState dict with ``supervisor_output``, ``token_usage``,
        ``execution_history``, ``errors``, and ``execution_times``.
    """
    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc)

    model_id = os.environ.get("GEMINI_MODEL_ID", DEFAULT_MODEL_ID)
    on_prem_endpoint = os.environ.get("GEMINI_ON_PREM_ENDPOINT")

    with agent_span(logger, AGENT_NAME, state["request_id"]) as span:
        try:
            fetched: Optional[FetchedData] = state.get("fetched_data")
            validation: Optional[ValidationResult] = state.get("validation_result")
            analysis: Optional[AnalysisResult] = state.get("analysis_result")

            if fetched is None or validation is None:
                raise ValueError(
                    "Supervisor requires fetched_data and validation_result"
                )

            supervisor_input = SupervisorInput(
                user_id=state["user_id"],
                tenant_id=state["tenant_id"],
                fetched_data=fetched,
                validation_result=validation,
                analysis_result=analysis,
            )

            user_prompt = _build_user_prompt(supervisor_input, model_id)

            raw_text, token_usage = _invoke_gemini(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                model_id=model_id,
                on_prem_endpoint=on_prem_endpoint,
            )

            log_token_usage(
                logger,
                AGENT_NAME,
                token_usage.prompt_tokens,
                token_usage.completion_tokens,
                model_id,
            )

            try:
                raw_dict = json.loads(raw_text)
            except json.JSONDecodeError as jde:
                raise RuntimeError(
                    f"Gemini returned non-JSON response: {raw_text[:200]}"
                ) from jde

            # Guardrail validation — may raise GuardrailViolation
            output = run_guardrails(raw_dict)

            duration_ms = (time.perf_counter() - start_time) * 1000
            span["model_id"] = model_id
            span["confidence_score"] = output.confidence_score
            span["recommendation_count"] = len(output.strategic_recommendations)

            return {
                "supervisor_output": output,
                "current_agent": AGENT_NAME,
                "token_usage": {AGENT_NAME: token_usage},
                "execution_history": [
                    AgentExecution(
                        agent_name=AGENT_NAME,
                        status=AgentStatus.SUCCESS,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        input_summary={"user_id": state["user_id"], "model": model_id},
                        output_summary={
                            "confidence": output.confidence_score,
                            "recommendations": len(output.strategic_recommendations),
                        },
                        platform=PLATFORM,
                    )
                ],
                "errors": [],
                "execution_times": {AGENT_NAME: round(duration_ms / 1000, 4)},
            }

        except (RuntimeError, ValueError, GuardrailViolation) as exc:
            import traceback as tb

            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "supervisor.failure_degraded",
                error=str(exc),
                model_id=model_id,
            )

            fallback_output = build_fallback_supervisor_output(
                user_id=state["user_id"],
                model_id=model_id,
                error_detail=str(exc),
            )

            return {
                "supervisor_output": fallback_output,
                "current_agent": AGENT_NAME,
                "token_usage": {},
                "execution_history": [
                    AgentExecution(
                        agent_name=AGENT_NAME,
                        status=AgentStatus.FAILURE,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        output_summary={"fallback": True},
                        platform=PLATFORM,
                    )
                ],
                "errors": [
                    AgentError(
                        agent_name=AGENT_NAME,
                        error_type=type(exc).__name__,
                        message=str(exc),
                        traceback=tb.format_exc(),
                        is_fatal=False,
                    )
                ],
                "execution_times": {AGENT_NAME: round(duration_ms / 1000, 4)},
            }
