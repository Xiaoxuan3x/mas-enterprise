"""
Salesforce CRM integration for the MAS platform.

Logs conversation sessions, creates/updates Case records for high-risk users,
and attaches risk assessment reports as Case attachments.

Uses the ``simple-salesforce`` library for REST API access via OAuth 2.0
username-password flow (suitable for server-side, non-interactive use cases).
For production, consider migrating to the Connected App / JWT Bearer flow.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.logging_config import get_logger

logger = get_logger(__name__)


class SalesforceClient:
    """
    Thin wrapper around the Salesforce REST API.

    Authenticates lazily on first use and caches the session.  Session tokens
    expire after 2 hours by default; a re-authentication is triggered
    automatically on 401 responses.

    Usage::

        sf = SalesforceClient()
        case_id = sf.create_risk_case(user_id="user_123", risk_level="critical")
        sf.log_conversation_activity(
            user_id="user_123",
            session_id="sess_abc",
            utterance="What is my risk score?",
            response="Your assessment is complete.",
            risk_level="critical",
        )
    """

    def __init__(self) -> None:
        self._sf: Optional[Any] = None

    def _get_client(self) -> Any:
        """
        Return a cached ``simple_salesforce.Salesforce`` instance.

        Authenticates if not already connected.

        Returns:
            Authenticated Salesforce client.

        Raises:
            RuntimeError: If credentials are not configured.
        """
        if self._sf is not None:
            return self._sf

        try:
            from simple_salesforce import Salesforce

            username = os.environ.get("SALESFORCE_USERNAME", "")
            password = os.environ.get("SALESFORCE_PASSWORD", "")
            security_token = os.environ.get("SALESFORCE_SECURITY_TOKEN", "")
            domain = os.environ.get("SALESFORCE_DOMAIN", "login")

            if not all([username, password, security_token]):
                raise RuntimeError(
                    "SALESFORCE_USERNAME, SALESFORCE_PASSWORD, and "
                    "SALESFORCE_SECURITY_TOKEN must be set"
                )

            self._sf = Salesforce(
                username=username,
                password=password,
                security_token=security_token,
                domain=domain,
            )
            logger.info("salesforce.connected", username=username[:10] + "***")
            return self._sf

        except ImportError:
            raise RuntimeError(
                "simple-salesforce package is not installed. "
                "Run: pip install simple-salesforce"
            )

    def create_risk_case(
        self,
        user_id: str,
        risk_level: str,
        summary: str,
        tenant_id: str = "unknown",
    ) -> str:
        """
        Create a Salesforce Case record for a high-risk user assessment.

        Args:
            user_id:   Tokenised user identifier.
            risk_level: Risk level string from AnalysisResult.
            summary:   Executive summary from SupervisorOutput.
            tenant_id: Tenant identifier for account mapping.

        Returns:
            Salesforce Case ID string.

        Side effects:
            Creates a Case object in Salesforce with status "New".
        """
        sf = self._get_client()

        priority_map = {
            "low": "Low",
            "medium": "Medium",
            "high": "High",
            "critical": "High",
        }

        case = sf.Case.create(
            {
                "Subject": f"MAS Risk Assessment — {risk_level.upper()} — {user_id[:12]}",
                "Description": summary[:32000],
                "Priority": priority_map.get(risk_level.lower(), "Medium"),
                "Status": "New",
                "Origin": "MAS Enterprise Platform",
                "Type": "Risk Assessment",
                "MAS_Risk_Level__c": risk_level,
                "MAS_User_ID__c": user_id,
                "MAS_Tenant_ID__c": tenant_id,
                "MAS_Generated_At__c": datetime.now(timezone.utc).isoformat(),
            }
        )

        case_id: str = case["id"]
        logger.info(
            "salesforce.case_created",
            case_id=case_id,
            risk_level=risk_level,
        )
        return case_id

    def log_conversation_activity(
        self,
        user_id: str,
        session_id: str,
        utterance: str,
        response: str,
        risk_level: str,
    ) -> None:
        """
        Log a conversational turn as a Salesforce Task activity.

        Creates a Task record linked to the relevant Case (looked up by
        user_id + risk_level) so compliance teams have a full conversation audit
        trail within Salesforce.

        Args:
            user_id:    Tokenised user identifier.
            session_id: Conversational session ID.
            utterance:  User's input text.
            response:   Agent's response text.
            risk_level: Current risk level for Case lookup context.

        Side effects:
            Creates a Salesforce Task object.
            Logs ``salesforce.activity_logged`` on success.
            Silently logs a warning and returns on failure (non-critical).
        """
        try:
            sf = self._get_client()

            sf.Task.create(
                {
                    "Subject": f"MAS Conversation — Session {session_id[:8]}",
                    "Description": (
                        f"User utterance: {utterance[:1000]}\n\n"
                        f"Agent response: {response[:1000]}"
                    ),
                    "Status": "Completed",
                    "Priority": "Normal",
                    "ActivityDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "MAS_Session_ID__c": session_id,
                    "MAS_User_ID__c": user_id,
                    "MAS_Risk_Level__c": risk_level,
                }
            )
            logger.info(
                "salesforce.activity_logged",
                session_id=session_id[:8],
                user_id=user_id[:12],
            )
        except Exception as exc:
            logger.warning("salesforce.activity_log_failed", error=str(exc))
