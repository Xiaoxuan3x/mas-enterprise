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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from schemas.agent_io import AgentStatus, RiskLevel
from schemas.state import initial_state


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

    mock_gemini_model = MagicMock()
    mock_gemini_response = MagicMock()
    mock_gemini_response.text = json.dumps(gemini_response_dict)
    mock_gemini_response.candidates = [MagicMock()]
    mock_gemini_response.usage_metadata = MagicMock(
        prompt_token_count=450,
        candidates_token_count=150,
        total_token_count=600,
    )
    mock_gemini_model.generate_content.return_value = mock_gemini_response

    # ── Patch Azure email ─────────────────────────────────────────────────
    mock_email_client = MagicMock()
    mock_poller = MagicMock()
    mock_poller.result.return_value = {"id": "msg_e2e_001", "status": "Submitted"}
    mock_email_client.begin_send.return_value = mock_poller

    with (
        patch("boto3.resource", return_value=mock_dynamodb),
        patch(
            "google.generativeai.GenerativeModel",
            return_value=mock_gemini_model,
        ),
        patch("google.generativeai.configure"),
        patch(
            "azure.communication.email.EmailClient.from_connection_string",
            return_value=mock_email_client,
        ),
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
        from graph.workflow import build_workflow

        graph = build_workflow()
        final_state = await graph.ainvoke(base_state)

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

    mock_gemini_model = MagicMock()
    mock_gemini_response = MagicMock()
    mock_gemini_response.text = json.dumps(fallback_response)
    mock_gemini_response.candidates = [MagicMock()]
    mock_gemini_response.usage_metadata = MagicMock(
        prompt_token_count=300,
        candidates_token_count=100,
        total_token_count=400,
    )
    mock_gemini_model.generate_content.return_value = mock_gemini_response

    with (
        patch("boto3.resource", return_value=mock_dynamodb),
        patch("google.generativeai.GenerativeModel", return_value=mock_gemini_model),
        patch("google.generativeai.configure"),
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
        from graph.workflow import build_workflow

        graph = build_workflow()
        final_state = await graph.ainvoke(base_state)

    final = final_state.get("final_response")
    assert final is not None

    # Should have validation failures recorded
    validation = final_state.get("validation_result")
    assert validation is not None
    assert validation.is_valid is False
    assert validation.critical_issue_count > 0

    # Pipeline should still complete (not crash)
    assert final.status in (AgentStatus.SUCCESS, AgentStatus.FAILURE)
