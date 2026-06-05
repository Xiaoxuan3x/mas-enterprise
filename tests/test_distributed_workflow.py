"""
Integration tests for the distributed HTTP-based MAS workflow.

These tests drive the LangGraph orchestrator against ASGI-hosted agent
services, which exercises the remote service contracts without requiring live
cloud infrastructure during CI.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from control_tower.config_manager import get_settings
pytest.importorskip("langgraph")
from graph.workflow import build_workflow
from schemas.agent_io import AgentStatus, EmailDeliveryReceipt
from services.agent_apps import agent_mesh_app


def _service_urls() -> dict[str, str]:
    """Return mounted ASGI base URLs matching the composite agent mesh app."""
    return {
        "data_fetcher": "http://agent-mesh/data-fetcher/",
        "data_validator": "http://agent-mesh/data-validator/",
        "analyst": "http://agent-mesh/analyst/",
        "supervisor": "http://agent-mesh/supervisor/",
        "email_agent": "http://agent-mesh/email-agent/",
        "conversational_agent": "http://agent-mesh/conversational-agent/",
    }


def _transport_map() -> dict[str, httpx.ASGITransport]:
    """Use one in-memory ASGI transport for every remote agent service."""
    return {agent_name: httpx.ASGITransport(app=agent_mesh_app) for agent_name in _service_urls()}


@pytest.mark.asyncio
async def test_distributed_workflow_success(base_state):
    """
    The distributed workflow should complete successfully over HTTP service calls.

    External SDK calls remain mocked, but the orchestrator now talks to remote
    agent endpoints instead of importing and calling them directly.
    """
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

    gemini_response_dict = {
        "user_id": base_state["user_id"],
        "executive_summary": (
            "The distributed workflow completed a low-risk assessment with normal "
            "transactional behaviour and no signals requiring immediate escalation."
        ),
        "strategic_recommendations": [
            {
                "priority": 1,
                "action": "Continue standard monitoring",
                "rationale": "Low composite score with no critical validation issues",
                "owner": "risk-operations",
                "timeline": "Ongoing",
            }
        ],
        "risk_narrative": "Signals remain within expected limits across the lookback window.",
        "next_steps": ["No action required"],
        "confidence_score": 0.9,
        "model_id": "gemini-2.5-pro",
    }

    email_receipt = EmailDeliveryReceipt(
        message_id="msg_distributed_001",
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
                "AWS_REGION": "us-east-1",
                "DYNAMODB_PROFILES_TABLE": "mas-user-profiles",
                "DYNAMODB_TRANSACTIONS_TABLE": "mas-transactions",
                "GEMINI_API_KEY": "test-api-key",
                "GEMINI_MODEL_ID": "gemini-2.5-pro",
                "AZURE_COMMUNICATION_CONNECTION_STRING": "endpoint=https://test;accesskey=abc123",
                "AZURE_EMAIL_SENDER": "no-reply@test.com",
                "INTER_SERVICE_HMAC_KEY": "test-distributed-shared-key",
            },
        ),
    ):
        get_settings.cache_clear()
        workflow = build_workflow(
            execution_mode="remote",
            base_url_overrides=_service_urls(),
            transport_overrides=_transport_map(),
        )
        final_state = await workflow.ainvoke(base_state)

    final = final_state.get("final_response")
    assert final is not None
    assert final.status == AgentStatus.SUCCESS
    assert final.risk_level is not None
    assert final.email_sent is True
    assert final.errors == []

    history = final_state.get("execution_history", [])
    agent_names = [entry.agent_name for entry in history]
    assert "data_fetcher" in agent_names
    assert "data_validator" in agent_names
    assert "analyst" in agent_names
    assert "supervisor" in agent_names


@pytest.mark.asyncio
async def test_distributed_workflow_supervisor_service_degrades(base_state):
    """
    Transported supervisor failures should still degrade gracefully.

    The remote service remains reachable, but the supervisor's internal Gemini
    call fails and returns the existing graceful degradation output.
    """
    state = {**base_state, "raw_input": {**base_state["raw_input"], "send_email": False}}

    mock_dynamodb = MagicMock()
    mock_profiles_table = MagicMock()
    mock_transactions_table = MagicMock()

    mock_profiles_table.get_item.return_value = {
        "Item": {
            "userId": state["user_id"],
            "tenantId": state["tenant_id"],
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
                "tenantId": state["tenant_id"],
            }
        ]
    }

    def table_side_effect(name):
        if "profile" in name.lower():
            return mock_profiles_table
        return mock_transactions_table

    mock_dynamodb.Table.side_effect = table_side_effect

    with (
        patch("agents.data_fetcher._build_dynamodb_client", return_value=mock_dynamodb),
        patch("agents.supervisor._invoke_gemini", side_effect=RuntimeError("Gemini request timed out")),
        patch.dict(
            "os.environ",
            {
                "AWS_REGION": "us-east-1",
                "DYNAMODB_PROFILES_TABLE": "mas-user-profiles",
                "DYNAMODB_TRANSACTIONS_TABLE": "mas-transactions",
                "INTER_SERVICE_HMAC_KEY": "test-distributed-shared-key",
            },
        ),
    ):
        get_settings.cache_clear()
        workflow = build_workflow(
            execution_mode="remote",
            base_url_overrides=_service_urls(),
            transport_overrides=_transport_map(),
        )
        final_state = await workflow.ainvoke(state)

    supervisor_output = final_state.get("supervisor_output")
    assert supervisor_output is not None
    assert supervisor_output.confidence_score == 0.0

    final = final_state.get("final_response")
    assert final is not None
    assert final.status == AgentStatus.SUCCESS
    assert any("timed out" in error.message.lower() for error in final_state.get("errors", []))
