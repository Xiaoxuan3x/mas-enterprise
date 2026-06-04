"""
Unit tests for the DataValidator deterministic rule engine.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.data_validator import _run_all_rules, _validate_profile, _validate_transaction
from schemas.agent_io import (
    FetchedData,
    Transaction,
    UserProfile,
    ValidationSeverity,
)


def _make_profile(**kwargs) -> UserProfile:
    defaults = dict(
        user_id="u1",
        full_name="Test",
        email="test@example.com",
        kyc_status="approved",
        account_age_days=100,
        country_of_residence="GB",
        risk_band="low",
    )
    return UserProfile(**{**defaults, **kwargs})


def _make_tx(**kwargs) -> Transaction:
    defaults = dict(
        transaction_id="tx1",
        amount=100.0,
        currency="USD",
        merchant_category="retail",
        timestamp=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
        country_code="US",
        is_card_present=True,
        channel="pos",
    )
    return Transaction(**{**defaults, **kwargs})


def test_valid_profile_has_no_issues():
    issues = _validate_profile(_make_profile())
    assert issues == []


def test_invalid_email_triggers_error():
    issues = _validate_profile(_make_profile(email="not-an-email"))
    severities = {i.severity for i in issues}
    assert ValidationSeverity.ERROR in severities


def test_unknown_kyc_status_triggers_critical():
    issues = _validate_profile(_make_profile(kyc_status="UNKNOWN"))
    assert any(i.severity == ValidationSeverity.CRITICAL for i in issues)


def test_negative_account_age_triggers_error():
    issues = _validate_profile(_make_profile(account_age_days=-1))
    assert any(i.field == "profile.account_age_days" for i in issues)


def test_invalid_country_code_triggers_error():
    issues = _validate_profile(_make_profile(country_of_residence="ZZZ"))
    assert len(issues) > 0


def test_valid_transaction_has_no_issues():
    issues = _validate_transaction(_make_tx(), 0)
    assert issues == []


def test_negative_amount_triggers_critical():
    issues = _validate_transaction(_make_tx(amount=-50.0), 0)
    assert any(i.severity == ValidationSeverity.CRITICAL for i in issues)


def test_future_timestamp_triggers_error():
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    issues = _validate_transaction(_make_tx(timestamp=future), 0)
    assert any(i.rule == "no_future_timestamp" for i in issues)


def test_invalid_currency_triggers_error():
    # Build a valid Transaction then mutate the field directly to bypass Pydantic
    tx = _make_tx()
    object.__setattr__(tx, "currency", "usd")  # bypass the validator post-construction
    issues = _validate_transaction(tx, 0)
    assert any("currency" in i.field for i in issues)


def test_run_all_rules_valid_data_returns_is_valid_true(
    fetched_data,
):
    result = _run_all_rules(fetched_data)
    assert result.is_valid is True
    assert result.critical_issue_count == 0


def test_run_all_rules_critical_issues_returns_is_valid_false(
    sample_profile,
    sample_transactions,
    user_id,
):
    bad_tx = _make_tx(amount=-999.0)
    data = FetchedData(
        user_id=user_id,
        profile=_make_profile(user_id=user_id, kyc_status="INVALID"),
        transactions=[bad_tx],
    )
    result = _run_all_rules(data)
    assert result.is_valid is False
    assert result.critical_issue_count > 0


def test_velocity_ceiling_critical_when_exceeded(user_id, sample_profile):
    """Total spend > 500,000 should trigger a critical velocity issue."""
    massive_txs = [
        _make_tx(transaction_id=f"tx_{i}", amount=100_000.0)
        for i in range(6)
    ]
    data = FetchedData(
        user_id=user_id,
        profile=sample_profile,
        transactions=massive_txs,
    )
    result = _run_all_rules(data)
    assert any(
        i.rule == "total_velocity_ceiling"
        and i.severity == ValidationSeverity.CRITICAL
        for i in result.issues
    )
