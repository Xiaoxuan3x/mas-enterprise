"""
Shared pytest fixtures for the MAS test suite.

Provides:
  - A fully-populated MASState for happy-path testing.
  - Individual agent I/O model fixtures.
  - Mock factories for cloud clients (DynamoDB, Azure, Gemini).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from schemas.agent_io import (
    AgentStatus,
    AnalysisResult,
    BacktestMetric,
    EmailDeliveryReceipt,
    FetchedData,
    FraudSignal,
    RiskLevel,
    SecurityContext,
    StrategicRecommendation,
    SupervisorOutput,
    Transaction,
    UserProfile,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
)
from schemas.state import MASState, initial_state


# ─────────────────────────────────────────────────────────────────────────────
# Base state fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def request_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def user_id() -> str:
    return "user_test_00001"


@pytest.fixture
def tenant_id() -> str:
    return "tenant_acme"


@pytest.fixture
def security_context() -> SecurityContext:
    return SecurityContext(
        subject="user_test_00001",
        tenant_id="tenant_acme",
        roles=["mas:analyse", "mas:read"],
        scopes=["mas:internal"],
        issuer="https://keycloak.internal/realms/mas",
        issued_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc),
    )


@pytest.fixture
def base_state(request_id, user_id, tenant_id, security_context) -> MASState:
    return initial_state(
        request_id=request_id,
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=str(uuid.uuid4()),
        raw_input={
            "user_id": user_id,
            "tenant_id": tenant_id,
            "fetch_types": ["profile", "transactions"],
            "date_range_days": 30,
            "send_email": True,
            "notification_email": "compliance@acme.com",
            "notification_name": "Compliance Team",
        },
        security_context=security_context,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Data model fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_profile(user_id) -> UserProfile:
    return UserProfile(
        user_id=user_id,
        full_name="Test User",
        email="test@acme.com",
        kyc_status="approved",
        account_age_days=365,
        country_of_residence="GB",
        risk_band="low",
    )


@pytest.fixture
def sample_transactions() -> list:
    return [
        Transaction(
            transaction_id="tx_001",
            amount=250.00,
            currency="GBP",
            merchant_category="retail",
            timestamp=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
            country_code="GB",
            is_card_present=True,
            channel="pos",
        ),
        Transaction(
            transaction_id="tx_002",
            amount=99.99,
            currency="GBP",
            merchant_category="online_retail",
            timestamp=datetime(2026, 5, 15, 14, 30, tzinfo=timezone.utc),
            country_code="GB",
            is_card_present=False,
            channel="online",
        ),
    ]


@pytest.fixture
def fetched_data(user_id, sample_profile, sample_transactions) -> FetchedData:
    return FetchedData(
        user_id=user_id,
        profile=sample_profile,
        transactions=sample_transactions,
    )


@pytest.fixture
def validation_result() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        issues=[],
        schema_version="1.0",
        rules_applied=["email_format", "kyc_status_enum"],
        transaction_count=2,
    )


@pytest.fixture
def analysis_result(user_id) -> AnalysisResult:
    return AnalysisResult(
        user_id=user_id,
        composite_risk_score=18.5,
        risk_level=RiskLevel.LOW,
        fraud_signals=[
            FraudSignal(
                signal_name="velocity_anomaly",
                score=0.15,
                weight=0.30,
                evidence="7-day spend within normal range",
            )
        ],
        backtest_metrics=[
            BacktestMetric(
                metric_name="composite_risk_score",
                value=18.5,
                benchmark=50.0,
                delta=-31.5,
                passed=True,
            )
        ],
        recommended_action="No immediate action required.",
        requires_human_review=False,
        model_version="rules-engine-v1.2",
        explanation="Low-risk assessment based on normal spending patterns.",
    )


@pytest.fixture
def supervisor_output(user_id) -> SupervisorOutput:
    return SupervisorOutput(
        user_id=user_id,
        executive_summary=(
            "The automated risk assessment for this customer indicates a low-risk "
            "profile with normal spending behaviour. No immediate action is required. "
            "Standard monitoring procedures should continue."
        ),
        strategic_recommendations=[
            StrategicRecommendation(
                priority=1,
                action="Continue standard monitoring",
                rationale="Low composite risk score with no critical signals",
                owner="risk-operations",
                timeline="Ongoing",
            )
        ],
        risk_narrative="All fraud signals within normal thresholds.",
        next_steps=["No action required"],
        confidence_score=0.92,
        model_id="gemini-2.5-pro",
        guardrail_passed=True,
    )


@pytest.fixture
def email_receipt() -> EmailDeliveryReceipt:
    return EmailDeliveryReceipt(
        message_id="msg_test_001",
        recipient_email="compliance@acme.com",
        status="Delivered",
        provider="azure-communication-services",
    )


# ─────────────────────────────────────────────────────────────────────────────
# State with all fields populated (for finalize/e2e tests)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def full_state(
    base_state,
    fetched_data,
    validation_result,
    analysis_result,
    supervisor_output,
    email_receipt,
) -> MASState:
    return {
        **base_state,
        "fetched_data": fetched_data,
        "validation_result": validation_result,
        "analysis_result": analysis_result,
        "supervisor_output": supervisor_output,
        "email_receipt": email_receipt,
        "execution_times": {
            "orchestrator": 0.001,
            "data_fetcher": 0.12,
            "data_validator": 0.05,
            "analyst": 0.08,
            "supervisor": 1.2,
            "email_agent": 0.3,
        },
    }
