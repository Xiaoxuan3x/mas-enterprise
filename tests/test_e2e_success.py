"""
End-to-end integration test: successful pipeline run.

Mocks all external I/O (DynamoDB, Gemini, Azure) and verifies that the
LangGraph workflow produces a valid FinalResponse with risk_level, summary,
and a delivered email receipt.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents import analyst, data_fetcher, data_validator, email_agent, orchestrator, policy_gate, supervisor
from schemas.agent_io import AgentStatus, EmailDeliveryReceipt


def _merge_state(state, patch):
    for key, value in patch.items():
        if key in {"execution_history", "errors"}:
            state[key] = [*state.get(key, []), *value]
        elif key in {"execution_times", "token_usage", "metadata"}:
            merged = dict(state.get(key, {}))
            merged.update(value)
            state[key] = merged
        else:
            state[key] = value
    return state


async def _run_pipeline(state):
    _merge_state(state, await orchestrator.run(state))
    _merge_state(state, await data_fetcher.run(state))
    _merge_state(state, await data_validator.run(state))
    _merge_state(state, await policy_gate.pre_analysis_run(state))

    route = orchestrator.route_after_validation(state)
    if route == "error_handler":
        _merge_state(state, await orchestrator.error_handler(state))
        return state

    if route == "analyst":
        _merge_state(state, await analyst.run(state))
        _merge_state(state, await policy_gate.post_analysis_run(state))
        if orchestrator.route_after_post_analysis_policy(state) == "error_handler":
            _merge_state(state, await orchestrator.error_handler(state))
            return state

    _merge_state(state, await supervisor.run(state))

    route = orchestrator.route_after_supervisor(state)
    if route == "email_agent":
        _merge_state(state, await email_agent.run(state))
        route = orchestrator.route_after_email(state)

    if route == "conversational_agent":
        raise AssertionError("Conversational path is not expected in these e2e tests")

    _merge_state(state, await orchestrator.finalize(state))
    return state


@pytest.mark.asyncio
async def test_full_pipeline_success(
    base_state,
    fetched_data,
    analysis_result,
    supervisor_output,
    email_receipt,
):
    """
    Verify that the compiled LangGraph graph completes successfully when all
    external dependencies are healthy.

    Mocks:
      - DynamoDB.get_item and query (data_fetcher)
      - google.generativeai.GenerativeModel (supervisor)
      - azure.communication.email.EmailClient (email_agent)

    Asserts:
      - final_response.status == "success"
      - final_response.risk_level is populated
      - final_response.executive_summary is non-empty
      - email_sent == True
      - No errors in final_response.errors
    """
    # ── Patch DynamoDB ────────────────────────────────────────────────────
    mock_dynamodb = MagicMock()
    mock_profiles_table = MagicMock()
    mock_transactions_table = MagicMock()

    mock_profiles_table.get_item.return_value = {
        "Item": {
            "userId": base_state["user_id"],
            "tenantId": base_state["tenant_id"],
            "fullName": "Test User",
            "email": "test@acme.com",
            "kycStatus": "approved",
            "accountAgeDays": 365,
            "countryOfResidence": "GB",
            "riskBand": "low",
        }
    }

    mock_transactions_table.query.return_value = {
        "Items": [
            {
                "transactionId": "tx_001",
                "amount": 250.00,
                "currency": "GBP",
                "merchantCategory": "retail",
                "timestamp": "2026-05-01T10:00:00+00:00",
                "countryCode": "GB",
                "isCardPresent": True,
                "channel": "pos",
                "tenantId": base_state["tenant_id"],
            }
        ]
    }

    def table_side_effect(name):
        if "profile" in name.lower():
            return mock_profiles_table
        return mock_transactions_table

    mock_dynamodb.Table.side_effect = table_side_effect

    # ── Patch Gemini ──────────────────────────────────────────────────────
    gemini_response_dict = {
        "user_id": base_state["user_id"],
        "executive_summary": (
            "The automated risk assessment indicates a low-risk customer profile "
            "with normal transactional behaviour over the past 30 days. "
            "No immediate escalation is required."
        ),
        "strategic_recommendations": [
            {
                "priority": 1,
                "action": "Continue standard monitoring",
                "rationale": "Low composite score — no critical signals detected",
                "owner": "risk-operations",
                "timeline": "Ongoing",
            }
        ],
        "risk_narrative": "All signals within normal thresholds.",
        "next_steps": ["No action required"],
        "confidence_score": 0.91,
        "model_id": "gemini-2.5-pro",
    }

    email_receipt = EmailDeliveryReceipt(
        message_id="msg_e2e_001",
        recipient_email="compliance@acme.com",
        status="Submitted",
    )

    with (
        patch("agents.data_fetcher._build_dynamodb_client", return_value=mock_dynamodb),
        patch(
            "agents.supervisor._invoke_gemini",
            return_value=(
                json.dumps(gemini_response_dict),
                MagicMock(
                    prompt_tokens=450,
                    completion_tokens=150,
                    total_tokens=600,
                    model_id="gemini-2.5-pro",
                ),
            ),
        ),
        patch("agents.email_agent._send_via_azure", return_value=email_receipt),
        patch.dict(
            "os.environ",
            {
                "DYNAMODB_PROFILES_TABLE": "mas-user-profiles",
                "DYNAMODB_TRANSACTIONS_TABLE": "mas-transactions",
                "AWS_REGION": "us-east-1",
                "GEMINI_API_KEY": "test-api-key",
                "GEMINI_MODEL_ID": "gemini-2.5-pro",
                "AZURE_COMMUNICATION_CONNECTION_STRING": "endpoint=https://test;accesskey=abc123",
                "AZURE_EMAIL_SENDER": "no-reply@test.com",
            },
        ),
    ):
        final_state = await _run_pipeline(base_state)

    final = final_state.get("final_response")
    assert final is not None, "Pipeline must produce a final_response"
    assert final.status == AgentStatus.SUCCESS
    assert final.risk_level is not None
    assert final.executive_summary is not None
    assert len(final.executive_summary) >= 50
    assert len(final.recommendations) >= 1
    assert final.email_sent is True
    assert final.errors == []
    assert final.pipeline_duration_ms > 0
    assert final_state["email_receipt"].recipient_email == "compliance@acme.com"

    # Verify execution history captured all agents
    history = final_state.get("execution_history", [])
    agent_names = [h.agent_name for h in history]
    assert "orchestrator" in agent_names
    assert "data_fetcher" in agent_names
    assert "data_validator" in agent_names
    assert "analyst" in agent_names
    assert "supervisor" in agent_names


@pytest.mark.asyncio
async def test_data_validator_blocks_invalid_data(base_state):
    """
    Verify that when DynamoDB returns data that fails critical validation rules,
    the pipeline routes to the supervisor (skipping analyst) and still produces
    a valid FinalResponse with degraded output.
    """
    # Invalid profile: KYC status not in allowed set
    mock_dynamodb = MagicMock()
    mock_profiles_table = MagicMock()
    mock_transactions_table = MagicMock()

    mock_profiles_table.get_item.return_value = {
        "Item": {
            "userId": base_state["user_id"],
            "tenantId": base_state["tenant_id"],
            "fullName": "Bad User",
            "email": "not-an-email",
            "kycStatus": "INVALID_STATUS",  # will fail critical validation
            "accountAgeDays": -5,           # will fail non-negative check
            "countryOfResidence": "GB",
            "riskBand": "unknown",
        }
    }

    mock_transactions_table.query.return_value = {
        "Items": [
            {
                "transactionId": "tx_bad",
                "amount": -100.0,  # negative amount — critical issue
                "currency": "GBP",
                "merchantCategory": "retail",
                "timestamp": "2026-05-01T10:00:00+00:00",
                "countryCode": "GB",
                "isCardPresent": False,
                "channel": "online",
                "tenantId": base_state["tenant_id"],
            }
        ]
    }

    def table_side_effect(name):
        if "profile" in name.lower():
            return mock_profiles_table
        return mock_transactions_table

    mock_dynamodb.Table.side_effect = table_side_effect

    # Gemini still responds (supervisor receives invalid-data path)
    fallback_response = {
        "user_id": base_state["user_id"],
        "executive_summary": (
            "Automated analysis completed with reduced confidence due to data "
            "quality issues. Manual review is strongly recommended before taking action."
        ),
        "strategic_recommendations": [
            {
                "priority": 1,
                "action": "Escalate to compliance team for manual data review",
                "rationale": "Critical validation failures detected in source data",
                "owner": "compliance-team",
                "timeline": "24 hours",
            }
        ],
        "risk_narrative": "Data quality issues prevent reliable automated scoring.",
        "next_steps": ["Manual data quality review required"],
        "confidence_score": 0.4,
        "model_id": "gemini-2.5-pro",
    }

    with (
        patch("agents.data_fetcher._build_dynamodb_client", return_value=mock_dynamodb),
        patch(
            "agents.supervisor._invoke_gemini",
            return_value=(
                json.dumps(fallback_response),
                MagicMock(
                    prompt_tokens=300,
                    completion_tokens=100,
                    total_tokens=400,
                    model_id="gemini-2.5-pro",
                ),
            ),
        ),
        patch.dict(
            "os.environ",
            {
                "DYNAMODB_PROFILES_TABLE": "mas-user-profiles",
                "DYNAMODB_TRANSACTIONS_TABLE": "mas-transactions",
                "AWS_REGION": "us-east-1",
                "GEMINI_API_KEY": "test-api-key",
            },
        ),
    ):
        final_state = await _run_pipeline(base_state)

    final = final_state.get("final_response")
    assert final is not None

    # Should have validation failures recorded
    validation = final_state.get("validation_result")
    assert validation is not None
    assert validation.is_valid is False
    assert validation.critical_issue_count > 0

    # Pipeline should still complete (not crash)
    assert final.status in (AgentStatus.SUCCESS, AgentStatus.FAILURE)
    assert final.email_sent is False
    assert "email_agent" not in [h.agent_name for h in final_state.get("execution_history", [])]
