"""
Analyst Agent (Deterministic Agent A) — Platform: AWS (Bedrock optional)

Performs risk scoring, fraud detection, and strategy backtesting on validated
transaction data.  The core scoring logic is deterministic (rule-based math);
an optional AWS Bedrock Claude model can enrich the narrative explanation.

Inputs:  FetchedData + ValidationResult from state.
Outputs: AnalysisResult (composite_risk_score, fraud_signals, backtest_metrics).
Platform: AWS — boto3 for DynamoDB/S3 lookups; optionally Bedrock for narrative.
Retry:   Up to 3 attempts with exponential backoff.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.retry import with_async_retry
from schemas.agent_io import (
    AgentError,
    AgentExecution,
    AgentStatus,
    AnalysisResult,
    BacktestMetric,
    FetchedData,
    FraudSignal,
    RiskLevel,
    Transaction,
    ValidationResult,
)
from schemas.state import MASState
from core.logging_config import agent_span, get_logger

logger = get_logger(__name__)

AGENT_NAME = "analyst"
PLATFORM = "aws"
MODEL_VERSION = "rules-engine-v1.2"


# ─────────────────────────────────────────────────────────────────────────────
# Fraud signal detectors  (deterministic, math-validated)
# ─────────────────────────────────────────────────────────────────────────────


def _signal_velocity(transactions: List[Transaction]) -> FraudSignal:
    """
    Detect anomalous spend velocity by comparing 7-day spend to 30-day average.

    Score formula:  ratio = spend_7d / (spend_30d / 30 * 7 + epsilon)
    Normalised to [0, 1] with sigmoid clamp.

    Args:
        transactions: Full transaction list from FetchedData.

    Returns:
        FraudSignal with name "velocity_anomaly".
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    spend_7d = sum(t.amount for t in transactions if t.timestamp >= cutoff_7d)
    spend_30d = sum(t.amount for t in transactions if t.timestamp >= cutoff_30d)

    epsilon = 1.0
    expected_7d = (spend_30d / 30.0) * 7.0
    ratio = spend_7d / (expected_7d + epsilon)

    # Sigmoid normalisation: score approaches 1 as ratio → ∞
    score = 1 - (1 / (1 + math.exp(ratio - 2.5)))
    score = round(min(max(score, 0.0), 1.0), 4)

    return FraudSignal(
        signal_name="velocity_anomaly",
        score=score,
        weight=0.30,
        evidence=(
            f"7-day spend: {spend_7d:.2f}, expected 7-day based on 30-day avg: "
            f"{expected_7d:.2f}, ratio: {ratio:.2f}"
        ),
    )


def _signal_geo_anomaly(transactions: List[Transaction]) -> FraudSignal:
    """
    Detect geographic dispersion: too many distinct countries in a short window.

    Score = min(distinct_countries / 5, 1.0)

    Args:
        transactions: Transaction list.

    Returns:
        FraudSignal with name "geo_anomaly".
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    recent = [t for t in transactions if t.timestamp >= cutoff]
    distinct_countries = len(set(t.country_code for t in recent))

    score = round(min(distinct_countries / 5.0, 1.0), 4)

    return FraudSignal(
        signal_name="geo_anomaly",
        score=score,
        weight=0.25,
        evidence=f"{distinct_countries} distinct countries in last 7 days",
    )


def _signal_night_transactions(transactions: List[Transaction]) -> FraudSignal:
    """
    Detect unusually high proportion of transactions between 00:00–05:00 UTC.

    Score = night_tx_count / max(total_tx_count, 1)

    Args:
        transactions: Transaction list.

    Returns:
        FraudSignal with name "night_activity".
    """
    night = [t for t in transactions if 0 <= t.timestamp.hour < 5]
    score = round(len(night) / max(len(transactions), 1), 4)

    return FraudSignal(
        signal_name="night_activity",
        score=score,
        weight=0.15,
        evidence=f"{len(night)} of {len(transactions)} transactions between 00:00-05:00 UTC",
    )


def _signal_large_round_amounts(transactions: List[Transaction]) -> FraudSignal:
    """
    Detect structuring: a high proportion of transactions with round amounts
    (multiples of 1000 with zero cents) is a common money-laundering indicator.

    Args:
        transactions: Transaction list.

    Returns:
        FraudSignal with name "round_amount_structuring".
    """
    round_amounts = [
        t for t in transactions
        if t.amount > 0 and t.amount % 1000 == 0 and t.amount <= 10_000
    ]
    score = round(len(round_amounts) / max(len(transactions), 1), 4)

    return FraudSignal(
        signal_name="round_amount_structuring",
        score=score,
        weight=0.30,
        evidence=f"{len(round_amounts)} round-amount transactions of {len(transactions)} total",
    )


def _compute_composite_score(signals: List[FraudSignal]) -> float:
    """
    Compute a weighted composite fraud risk score from individual signals.

    Formula: Σ(signal.score × signal.weight) / Σ(signal.weight) × 100

    Args:
        signals: List of FraudSignal objects with score and weight fields.

    Returns:
        Composite score in [0.0, 100.0].
    """
    weighted_sum = sum(s.score * s.weight for s in signals)
    weight_total = sum(s.weight for s in signals)
    if weight_total == 0:
        return 0.0
    return round((weighted_sum / weight_total) * 100, 4)


def _classify_risk(score: float, kyc_status: str) -> RiskLevel:
    """
    Map a composite score to a RiskLevel enum, boosting non-KYC-approved users.

    Args:
        score:      Composite risk score in [0, 100].
        kyc_status: User's KYC status from the profile.

    Returns:
        RiskLevel enum value.
    """
    kyc_boost = 10.0 if kyc_status != "approved" else 0.0
    adjusted = min(score + kyc_boost, 100.0)

    if adjusted < 20:
        return RiskLevel.LOW
    if adjusted < 50:
        return RiskLevel.MEDIUM
    if adjusted < 75:
        return RiskLevel.HIGH
    return RiskLevel.CRITICAL


# ─────────────────────────────────────────────────────────────────────────────
# Backtesting
# ─────────────────────────────────────────────────────────────────────────────


def _run_backtest(
    transactions: List[Transaction], risk_score: float
) -> List[BacktestMetric]:
    """
    Execute deterministic backtesting metrics to evaluate model performance.

    Compares the current scoring run against hardcoded benchmarks derived
    from historical model calibration.  All thresholds are sourced from
    compliance policy documents, not learned heuristics.

    Args:
        transactions: Transaction list for the analysis window.
        risk_score:   Composite fraud risk score from signal aggregation.

    Returns:
        List of BacktestMetric objects with pass/fail status.
    """
    metrics: List[BacktestMetric] = []

    avg_tx_amount = (
        sum(t.amount for t in transactions) / len(transactions)
        if transactions else 0.0
    )
    metrics.append(
        BacktestMetric(
            metric_name="avg_transaction_amount",
            value=round(avg_tx_amount, 2),
            benchmark=500.0,
            delta=round(avg_tx_amount - 500.0, 2),
            passed=avg_tx_amount <= 5000.0,
        )
    )

    tx_count = len(transactions)
    metrics.append(
        BacktestMetric(
            metric_name="transaction_count_30d",
            value=float(tx_count),
            benchmark=100.0,
            delta=float(tx_count - 100),
            passed=tx_count <= 500,
        )
    )

    metrics.append(
        BacktestMetric(
            metric_name="composite_risk_score",
            value=round(risk_score, 4),
            benchmark=50.0,
            delta=round(risk_score - 50.0, 4),
            passed=risk_score < 75.0,
        )
    )

    return metrics


# ─────────────────────────────────────────────────────────────────────────────
# Optional Bedrock enrichment (narrative explanation only)
# ─────────────────────────────────────────────────────────────────────────────


def _enrich_explanation_via_bedrock(
    risk_score: float,
    risk_level: RiskLevel,
    signals: List[FraudSignal],
    bedrock_model_id: str,
    aws_region: str,
) -> str:
    """
    Use AWS Bedrock (Claude) to generate a human-readable explanation for the
    deterministic risk score.  The LLM is used ONLY for prose generation —
    the scoring decisions are made exclusively by the deterministic rules above.

    Args:
        risk_score:       Composite risk score [0–100].
        risk_level:       Classified risk level enum.
        signals:          Fraud signals with scores and evidence.
        bedrock_model_id: Bedrock model ID (e.g., "anthropic.claude-3-5-sonnet-20241022-v2:0").
        aws_region:       AWS region for the Bedrock client.

    Returns:
        A one-paragraph plain-English explanation of the risk assessment.

    Side effects:
        Makes a synchronous Bedrock InvokeModel API call.  Returns a fallback
        string if Bedrock is unavailable rather than raising.
    """
    import boto3
    import json as _json

    signal_lines = "\n".join(
        f"- {s.signal_name}: score={s.score:.2f}, evidence={s.evidence}"
        for s in signals
    )
    prompt = (
        f"You are a fraud risk analyst. Summarise the following automated risk "
        f"assessment in one clear paragraph for a compliance officer. "
        f"Risk score: {risk_score:.1f}/100. Risk level: {risk_level.value}.\n"
        f"Signals detected:\n{signal_lines}\n"
        f"Do not reveal exact thresholds or model internals."
    )

    try:
        client = boto3.client("bedrock-runtime", region_name=aws_region)
        body = _json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        response = client.invoke_model(modelId=bedrock_model_id, body=body)
        result = _json.loads(response["body"].read())
        return result["content"][0]["text"].strip()
    except Exception as exc:
        logger.warning(
            "analyst.bedrock_enrichment_failed",
            error=str(exc),
        )
        return (
            f"Automated risk assessment completed. Composite score: "
            f"{risk_score:.1f}/100 ({risk_level.value} risk)."
        )


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node
# ─────────────────────────────────────────────────────────────────────────────


@with_async_retry(agent_name=AGENT_NAME)
async def _run_analysis_with_retry(
    fetched: FetchedData,
    validation: Optional[ValidationResult],
    use_bedrock: bool,
    bedrock_model_id: str,
    aws_region: str,
) -> AnalysisResult:
    """
    Execute the deterministic analyst workflow with a retry boundary.

    Args:
        fetched:           Structured customer/profile/transaction data.
        validation:        Optional validation result from the validator.
        use_bedrock:       Whether narrative enrichment is enabled.
        bedrock_model_id:  Bedrock model ID for optional explanation enrichment.
        aws_region:        AWS region for optional Bedrock calls.

    Returns:
        Fully-populated ``AnalysisResult``.
    """
    kyc_status = fetched.profile.kyc_status
    transactions = fetched.transactions
    signals = [
        _signal_velocity(transactions),
        _signal_geo_anomaly(transactions),
        _signal_night_transactions(transactions),
        _signal_large_round_amounts(transactions),
    ]

    composite_score = _compute_composite_score(signals)
    risk_level = _classify_risk(composite_score, kyc_status)
    backtest_metrics = _run_backtest(transactions, composite_score)

    if use_bedrock:
        explanation = _enrich_explanation_via_bedrock(
            risk_score=composite_score,
            risk_level=risk_level,
            signals=signals,
            bedrock_model_id=bedrock_model_id,
            aws_region=aws_region,
        )
    else:
        explanation = (
            f"Composite risk score {composite_score:.1f}/100 based on "
            f"{len(signals)} weighted fraud signals across "
            f"{len(transactions)} transactions."
        )

    requires_human = (
        risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        or (validation is not None and not validation.is_valid)
    )

    recommended_action = {
        RiskLevel.LOW: "No immediate action required. Proceed with standard monitoring.",
        RiskLevel.MEDIUM: "Apply enhanced monitoring. Review within 5 business days.",
        RiskLevel.HIGH: "Temporarily restrict account. Escalate to compliance team.",
        RiskLevel.CRITICAL: "Immediate account suspension. Alert fraud operations centre.",
    }[risk_level]

    return AnalysisResult(
        user_id=fetched.user_id,
        composite_risk_score=composite_score,
        risk_level=risk_level,
        fraud_signals=signals,
        backtest_metrics=backtest_metrics,
        recommended_action=recommended_action,
        requires_human_review=requires_human,
        model_version=MODEL_VERSION,
        explanation=explanation,
    )


async def run(state: MASState) -> Dict[str, Any]:
    """
    LangGraph node function for the Analyst agent.

    Runs fraud signal detection, composite scoring, risk classification, and
    backtesting.  Optionally enriches the explanation via AWS Bedrock.

    Args:
        state: Current MASState containing ``fetched_data`` and
               ``validation_result``.

    Returns:
        Partial MASState dict with ``analysis_result``, ``execution_history``,
        ``errors``, and ``execution_times`` populated.
    """
    import os

    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc)

    with agent_span(logger, AGENT_NAME, state["request_id"]) as span:
        try:
            fetched: Optional[FetchedData] = state.get("fetched_data")
            if fetched is None:
                raise ValueError("fetched_data is required for Analyst")

            validation: Optional[ValidationResult] = state.get("validation_result")
            use_bedrock = os.environ.get("ANALYST_USE_BEDROCK", "false").lower() == "true"
            result = await _run_analysis_with_retry(
                fetched=fetched,
                validation=validation,
                use_bedrock=use_bedrock,
                bedrock_model_id=os.environ.get(
                    "BEDROCK_ANALYST_MODEL",
                    "anthropic.claude-3-5-sonnet-20241022-v2:0",
                ),
                aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            )

            duration_ms = (time.perf_counter() - start_time) * 1000
            span["risk_level"] = result.risk_level.value
            span["composite_score"] = result.composite_risk_score

            return {
                "analysis_result": result,
                "current_agent": AGENT_NAME,
                "execution_history": [
                    AgentExecution(
                        agent_name=AGENT_NAME,
                        status=AgentStatus.SUCCESS,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        input_summary={"user_id": fetched.user_id},
                        output_summary={
                            "risk_level": result.risk_level.value,
                            "composite_score": result.composite_risk_score,
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
                "analysis_result": None,
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
