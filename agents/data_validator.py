"""
DataValidator Agent (Deterministic Agent B) — Platform: On-Prem (NVIDIA GPU)

A strict, rule-based agent that validates FetchedData using:
  - Pydantic v2 schema enforcement (structural validation)
  - Regex pattern checks (email, currency, country code)
  - Mathematical/business-rule checks (amount ranges, velocity limits)
  - Cross-field consistency checks (e.g., KYC status vs. account age)

This agent intentionally contains no LLM calls.  An optional SLM endpoint
(local vLLM on NVIDIA) can be used for semantic rule descriptions only —
the validation decisions themselves are fully deterministic.

Inputs:  FetchedData from the DataFetcher agent.
Outputs: ValidationResult (is_valid, issues list, rules_applied).
Retry:   Up to 3 attempts (guards against transient SLM endpoint failures).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from schemas.agent_io import (
    AgentError,
    AgentExecution,
    AgentStatus,
    FetchedData,
    Transaction,
    UserProfile,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
)
from schemas.state import MASState
from core.logging_config import agent_span, get_logger

logger = get_logger(__name__)

AGENT_NAME = "data_validator"
PLATFORM = "on-prem"
SCHEMA_VERSION = "1.0"

# ─────────────────────────────────────────────────────────────────────────────
# Validation rule constants
# ─────────────────────────────────────────────────────────────────────────────

_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
_CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")
_COUNTRY_REGEX = re.compile(r"^[A-Z]{2}$")

# Maximum single-transaction amount (USD equivalent) before flagging
_MAX_SINGLE_TX_AMOUNT = 50_000.0
# Maximum velocity: total spend over all transactions
_MAX_VELOCITY_AMOUNT = 500_000.0
# Minimum account age for high-value transactions
_MIN_ACCOUNT_AGE_FOR_HIGH_VALUE = 30
_HIGH_VALUE_THRESHOLD = 10_000.0

VALID_KYC_STATUSES = {"approved", "pending", "enhanced_due_diligence"}
VALID_CHANNELS = {"online", "pos", "atm", "mobile", "branch", "api", "none"}


# ─────────────────────────────────────────────────────────────────────────────
# Individual rule functions
# ─────────────────────────────────────────────────────────────────────────────


def _validate_profile(profile: UserProfile) -> List[ValidationIssue]:
    """
    Apply deterministic rules to the user profile fields.

    Args:
        profile: UserProfile model from FetchedData.

    Returns:
        List of ValidationIssue records for any failing checks.
    """
    issues: List[ValidationIssue] = []

    if profile.email and not _EMAIL_REGEX.match(profile.email):
        issues.append(
            ValidationIssue(
                field="profile.email",
                rule="email_format",
                severity=ValidationSeverity.ERROR,
                message="Email address does not match RFC-5322 pattern",
                observed_value=profile.email[:50],
            )
        )

    if profile.kyc_status not in VALID_KYC_STATUSES:
        issues.append(
            ValidationIssue(
                field="profile.kyc_status",
                rule="kyc_status_enum",
                severity=ValidationSeverity.CRITICAL,
                message=f"KYC status '{profile.kyc_status}' is not a recognised value",
                observed_value=profile.kyc_status,
            )
        )

    if not _COUNTRY_REGEX.match(profile.country_of_residence):
        issues.append(
            ValidationIssue(
                field="profile.country_of_residence",
                rule="iso3166_alpha2",
                severity=ValidationSeverity.ERROR,
                message="Country code must be ISO 3166-1 alpha-2 (2 uppercase letters)",
                observed_value=profile.country_of_residence,
            )
        )

    if profile.account_age_days < 0:
        issues.append(
            ValidationIssue(
                field="profile.account_age_days",
                rule="non_negative_age",
                severity=ValidationSeverity.ERROR,
                message="Account age cannot be negative",
                observed_value=str(profile.account_age_days),
            )
        )

    return issues


def _validate_transaction(tx: Transaction, idx: int) -> List[ValidationIssue]:
    """
    Apply deterministic rules to a single transaction record.

    Args:
        tx:  Transaction model.
        idx: Zero-based index in the transactions list (for error context).

    Returns:
        List of ValidationIssue records for any failing checks.
    """
    issues: List[ValidationIssue] = []
    prefix = f"transactions[{idx}]"

    if not _CURRENCY_REGEX.match(tx.currency):
        issues.append(
            ValidationIssue(
                field=f"{prefix}.currency",
                rule="iso4217_alpha3",
                severity=ValidationSeverity.ERROR,
                message="Currency must be ISO 4217 3-letter code",
                observed_value=tx.currency,
            )
        )

    if not _COUNTRY_REGEX.match(tx.country_code) and tx.country_code != "XX":
        issues.append(
            ValidationIssue(
                field=f"{prefix}.country_code",
                rule="iso3166_alpha2",
                severity=ValidationSeverity.WARNING,
                message="Country code must be ISO 3166-1 alpha-2",
                observed_value=tx.country_code,
            )
        )

    if tx.amount < 0:
        issues.append(
            ValidationIssue(
                field=f"{prefix}.amount",
                rule="non_negative_amount",
                severity=ValidationSeverity.CRITICAL,
                message="Transaction amount cannot be negative",
                observed_value=str(tx.amount),
            )
        )
    elif tx.amount > _MAX_SINGLE_TX_AMOUNT:
        issues.append(
            ValidationIssue(
                field=f"{prefix}.amount",
                rule="single_tx_ceiling",
                severity=ValidationSeverity.WARNING,
                message=f"Amount {tx.amount} exceeds single-transaction ceiling "
                        f"{_MAX_SINGLE_TX_AMOUNT}",
                observed_value=str(tx.amount),
            )
        )

    if tx.channel.lower() not in VALID_CHANNELS:
        issues.append(
            ValidationIssue(
                field=f"{prefix}.channel",
                rule="channel_enum",
                severity=ValidationSeverity.WARNING,
                message=f"Unrecognised transaction channel '{tx.channel}'",
                observed_value=tx.channel,
            )
        )

    if tx.timestamp > datetime.now(timezone.utc):
        issues.append(
            ValidationIssue(
                field=f"{prefix}.timestamp",
                rule="no_future_timestamp",
                severity=ValidationSeverity.ERROR,
                message="Transaction timestamp is in the future",
                observed_value=tx.timestamp.isoformat(),
            )
        )

    return issues


def _validate_cross_field(
    profile: UserProfile, transactions: List[Transaction]
) -> List[ValidationIssue]:
    """
    Apply cross-entity consistency rules.

    Args:
        profile:      UserProfile from FetchedData.
        transactions: Transaction list from FetchedData.

    Returns:
        List of cross-field ValidationIssue records.
    """
    issues: List[ValidationIssue] = []

    total_spend = sum(t.amount for t in transactions)
    if total_spend > _MAX_VELOCITY_AMOUNT:
        issues.append(
            ValidationIssue(
                field="transactions.velocity",
                rule="total_velocity_ceiling",
                severity=ValidationSeverity.CRITICAL,
                message=f"Total transaction volume {total_spend:.2f} exceeds "
                        f"velocity ceiling {_MAX_VELOCITY_AMOUNT}",
                observed_value=str(round(total_spend, 2)),
            )
        )

    high_value_txs = [t for t in transactions if t.amount >= _HIGH_VALUE_THRESHOLD]
    if (
        high_value_txs
        and profile.account_age_days < _MIN_ACCOUNT_AGE_FOR_HIGH_VALUE
        and profile.kyc_status != "enhanced_due_diligence"
    ):
        issues.append(
            ValidationIssue(
                field="profile.account_age_days",
                rule="new_account_high_value",
                severity=ValidationSeverity.WARNING,
                message=(
                    f"Account is only {profile.account_age_days} days old "
                    f"but has {len(high_value_txs)} high-value transaction(s). "
                    f"Enhanced due diligence is recommended."
                ),
                observed_value=str(profile.account_age_days),
            )
        )

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Main validation orchestration
# ─────────────────────────────────────────────────────────────────────────────


def _run_all_rules(fetched: FetchedData) -> ValidationResult:
    """
    Execute all validation rules against a FetchedData instance.

    Args:
        fetched: FetchedData produced by the DataFetcher agent.

    Returns:
        ValidationResult with is_valid flag and accumulated issues.
    """
    all_issues: List[ValidationIssue] = []
    rules_applied: List[str] = []

    # Profile rules
    profile_issues = _validate_profile(fetched.profile)
    all_issues.extend(profile_issues)
    rules_applied.extend(
        [
            "email_format",
            "kyc_status_enum",
            "iso3166_alpha2_residence",
            "non_negative_age",
        ]
    )

    # Per-transaction rules
    for idx, tx in enumerate(fetched.transactions):
        tx_issues = _validate_transaction(tx, idx)
        all_issues.extend(tx_issues)
    rules_applied.extend(
        [
            "iso4217_alpha3",
            "iso3166_alpha2_tx",
            "non_negative_amount",
            "single_tx_ceiling",
            "channel_enum",
            "no_future_timestamp",
        ]
    )

    # Cross-field rules
    cross_issues = _validate_cross_field(fetched.profile, fetched.transactions)
    all_issues.extend(cross_issues)
    rules_applied.extend(["total_velocity_ceiling", "new_account_high_value"])

    has_critical = any(
        i.severity == ValidationSeverity.CRITICAL for i in all_issues
    )
    has_error = any(i.severity == ValidationSeverity.ERROR for i in all_issues)
    is_valid = not has_critical and not has_error

    return ValidationResult(
        is_valid=is_valid,
        issues=all_issues,
        schema_version=SCHEMA_VERSION,
        rules_applied=list(set(rules_applied)),
        transaction_count=len(fetched.transactions),
    )


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node
# ─────────────────────────────────────────────────────────────────────────────


async def run(state: MASState) -> Dict[str, Any]:
    """
    LangGraph node function for the DataValidator agent.

    Validates the FetchedData payload using deterministic rules.  If no
    FetchedData is present in state (upstream failure), emits a critical
    validation error without crashing the graph.

    Args:
        state: Current MASState, must contain ``fetched_data``.

    Returns:
        Partial MASState dict with ``validation_result``, ``execution_history``,
        ``errors``, and ``execution_times`` fields populated.

    Side effects:
        Emits structured log events via the ``agent_span`` context manager.
    """
    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc)

    with agent_span(logger, AGENT_NAME, state["request_id"]) as span:
        try:
            fetched_data: FetchedData | None = state.get("fetched_data")
            if fetched_data is None:
                raise ValueError(
                    "fetched_data is None — DataFetcher must succeed before DataValidator"
                )

            result = _run_all_rules(fetched_data)

            duration_ms = (time.perf_counter() - start_time) * 1000
            span["is_valid"] = result.is_valid
            span["issue_count"] = len(result.issues)
            span["critical_count"] = result.critical_issue_count

            logger.info(
                "data_validator.result",
                is_valid=result.is_valid,
                issue_count=len(result.issues),
                critical_count=result.critical_issue_count,
            )

            return {
                "validation_result": result,
                "current_agent": AGENT_NAME,
                "execution_history": [
                    AgentExecution(
                        agent_name=AGENT_NAME,
                        status=AgentStatus.SUCCESS,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        input_summary={"user_id": state["user_id"]},
                        output_summary={
                            "is_valid": result.is_valid,
                            "issue_count": len(result.issues),
                        },
                        platform=PLATFORM,
                    )
                ],
                "errors": [],
                "execution_times": {AGENT_NAME: round(duration_ms / 1000, 4)},
            }

        except Exception as exc:
            import traceback

            duration_ms = (time.perf_counter() - start_time) * 1000
            return {
                "validation_result": ValidationResult(
                    is_valid=False,
                    issues=[
                        ValidationIssue(
                            field="system",
                            rule="agent_exception",
                            severity=ValidationSeverity.CRITICAL,
                            message=str(exc),
                        )
                    ],
                    schema_version=SCHEMA_VERSION,
                    rules_applied=[],
                    transaction_count=0,
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
