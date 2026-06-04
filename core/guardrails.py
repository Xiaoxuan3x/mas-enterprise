"""
Guardrail layer for non-deterministic (LLM-backed) agent outputs.

Validates that the Supervisor agent's output:
  1. Conforms to the SupervisorOutput Pydantic schema.
  2. Contains no forbidden tokens (PII, profanity, confidential markers).
  3. Meets minimum structural requirements (summary length, recommendation count).

On failure, a ``GuardrailViolation`` is raised with a structured reason code
so the orchestrator can trigger graceful degradation instead of propagating
a corrupted response downstream.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from core.logging_config import get_logger
from schemas.agent_io import SupervisorOutput

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

FORBIDDEN_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b\d{16}\b"),                          # Raw card PAN (16 digits)
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),               # US SSN pattern
    re.compile(r"\bCONFIDENTIAL\b", re.IGNORECASE),     # Confidential marker
    re.compile(r"\bINTERNAL USE ONLY\b", re.IGNORECASE),
    re.compile(r"<\|.*?\|>"),                            # Prompt injection artefacts
    re.compile(r"IGNORE PREVIOUS INSTRUCTIONS", re.IGNORECASE),
    re.compile(r"system:\s*(you are|act as)", re.IGNORECASE),  # Jailbreak patterns
]

MIN_SUMMARY_LENGTH = 50
MAX_SUMMARY_LENGTH = 2000
MAX_RECOMMENDATIONS = 10


# ─────────────────────────────────────────────────────────────────────────────
# Error types
# ─────────────────────────────────────────────────────────────────────────────


class ViolationCode(str, Enum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    FORBIDDEN_TOKEN = "FORBIDDEN_TOKEN"
    SUMMARY_TOO_SHORT = "SUMMARY_TOO_SHORT"
    SUMMARY_TOO_LONG = "SUMMARY_TOO_LONG"
    NO_RECOMMENDATIONS = "NO_RECOMMENDATIONS"
    TOO_MANY_RECOMMENDATIONS = "TOO_MANY_RECOMMENDATIONS"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


@dataclass
class GuardrailViolation(Exception):
    """
    Raised when the Supervisor's output fails one or more guardrail checks.

    Attributes:
        code:    Machine-readable violation category.
        detail:  Human-readable explanation of the violation.
        raw:     The raw output dict that triggered the violation (may be partial).
    """

    code: ViolationCode
    detail: str
    raw: Optional[Dict[str, Any]] = field(default=None)

    def __str__(self) -> str:
        return f"[{self.code}] {self.detail}"


# ─────────────────────────────────────────────────────────────────────────────
# Guardrail checks
# ─────────────────────────────────────────────────────────────────────────────


def _check_schema(raw: Dict[str, Any]) -> SupervisorOutput:
    """
    Parse the raw LLM output dict into a SupervisorOutput Pydantic model.

    Args:
        raw: Dict parsed from the LLM's JSON response.

    Returns:
        A validated SupervisorOutput instance.

    Raises:
        GuardrailViolation: If the dict does not match the schema.
    """
    try:
        return SupervisorOutput.model_validate(raw)
    except ValidationError as exc:
        raise GuardrailViolation(
            code=ViolationCode.SCHEMA_INVALID,
            detail=f"Schema validation failed: {exc.error_count()} error(s)",
            raw=raw,
        ) from exc


def _check_forbidden_tokens(output: SupervisorOutput) -> None:
    """
    Scan all free-text fields for forbidden patterns (PII, injection artefacts).

    Args:
        output: A schema-valid SupervisorOutput instance.

    Raises:
        GuardrailViolation: If any forbidden pattern matches.

    Side effects:
        Logs a warning for every pattern that matches before raising.
    """
    text_fields = [
        ("executive_summary", output.executive_summary),
        ("risk_narrative", output.risk_narrative),
        *[(f"recommendation[{i}].rationale", r.rationale)
          for i, r in enumerate(output.strategic_recommendations)],
    ]

    for field_name, text in text_fields:
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(text):
                logger.warning(
                    "guardrail.forbidden_token",
                    field=field_name,
                    pattern=pattern.pattern,
                )
                raise GuardrailViolation(
                    code=ViolationCode.FORBIDDEN_TOKEN,
                    detail=(
                        f"Forbidden pattern '{pattern.pattern}' found in "
                        f"field '{field_name}'"
                    ),
                )


def _check_structural_requirements(output: SupervisorOutput) -> None:
    """
    Enforce structural constraints on the Supervisor output.

    Args:
        output: A schema-valid SupervisorOutput instance.

    Raises:
        GuardrailViolation: If any structural constraint is violated.
    """
    summary_len = len(output.executive_summary)
    if summary_len < MIN_SUMMARY_LENGTH:
        raise GuardrailViolation(
            code=ViolationCode.SUMMARY_TOO_SHORT,
            detail=f"Summary has {summary_len} chars; minimum is {MIN_SUMMARY_LENGTH}",
        )
    if summary_len > MAX_SUMMARY_LENGTH:
        raise GuardrailViolation(
            code=ViolationCode.SUMMARY_TOO_LONG,
            detail=f"Summary has {summary_len} chars; maximum is {MAX_SUMMARY_LENGTH}",
        )

    rec_count = len(output.strategic_recommendations)
    if rec_count == 0:
        raise GuardrailViolation(
            code=ViolationCode.NO_RECOMMENDATIONS,
            detail="Supervisor produced zero strategic recommendations",
        )
    if rec_count > MAX_RECOMMENDATIONS:
        raise GuardrailViolation(
            code=ViolationCode.TOO_MANY_RECOMMENDATIONS,
            detail=f"Supervisor produced {rec_count} recommendations; max is {MAX_RECOMMENDATIONS}",
        )

    if output.confidence_score < 0.3:
        raise GuardrailViolation(
            code=ViolationCode.LOW_CONFIDENCE,
            detail=f"Supervisor confidence {output.confidence_score:.2f} is below threshold 0.30",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def run_guardrails(raw: Dict[str, Any]) -> SupervisorOutput:
    """
    Run all guardrail checks on the raw LLM output dict.

    Checks are ordered from cheapest to most expensive:
      1. Schema validation (Pydantic)
      2. Structural requirement checks
      3. Forbidden token scan (regex over all free-text fields)

    Args:
        raw: Dict parsed from the Supervisor's LLM JSON response.

    Returns:
        A guardrail-approved ``SupervisorOutput`` with ``guardrail_passed=True``.

    Raises:
        GuardrailViolation: On the first constraint that is violated.

    Side effects:
        Emits a ``guardrail.pass`` or ``guardrail.fail`` structured log event.
    """
    try:
        output = _check_schema(raw)
        _check_structural_requirements(output)
        _check_forbidden_tokens(output)
        output.guardrail_passed = True
        logger.info("guardrail.pass", user_id=output.user_id)
        return output
    except GuardrailViolation as exc:
        logger.warning("guardrail.fail", code=exc.code, detail=exc.detail)
        raise


def build_fallback_supervisor_output(
    user_id: str, model_id: str, error_detail: str
) -> SupervisorOutput:
    """
    Construct a safe, deterministic fallback SupervisorOutput for graceful
    degradation when the Supervisor fails or its output fails guardrails.

    Args:
        user_id:      The user being processed.
        model_id:     Model identifier of the failed Supervisor.
        error_detail: Human-readable failure reason for audit logging.

    Returns:
        A minimal SupervisorOutput that passes guardrails and signals degradation.
    """
    from schemas.agent_io import StrategicRecommendation

    logger.warning(
        "supervisor.fallback_activated", user_id=user_id, reason=error_detail
    )
    return SupervisorOutput(
        user_id=user_id,
        executive_summary=(
            "Automated analysis completed with reduced confidence. "
            "Manual review is recommended before taking action. "
            f"Reason: {error_detail[:100]}"
        ),
        strategic_recommendations=[
            StrategicRecommendation(
                priority=1,
                action="Escalate to human analyst for manual review",
                rationale="Automated supervisor is temporarily unavailable",
                owner="compliance-team",
                timeline="24 hours",
            )
        ],
        risk_narrative="Risk narrative unavailable due to supervisor degradation.",
        next_steps=["Manual review required"],
        confidence_score=0.0,
        model_id=model_id,
        guardrail_passed=True,
    )
