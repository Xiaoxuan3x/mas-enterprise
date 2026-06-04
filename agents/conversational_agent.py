"""
Conversational Agent — Platform: GCP (Vertex AI Agent Builder / Dialogflow CX)

Handles multi-turn conversational interactions about the risk assessment.
Routes user utterances through a Dialogflow CX agent for intent detection and
fulfillment.  For complex analytical follow-up questions, falls back to a
Gemini-backed response using the Vertex AI Generative Models API.

Integrations:
  - Salesforce: Logs conversation sessions and case updates.
  - GCP Logging: All conversation turns are emitted to Cloud Logging.

Platform: GCP
Inputs:   ConversationalInput (session_id, user_utterance, context).
Outputs:  ConversationalResponse (fulfillment_text, intent, parameters).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from schemas.agent_io import (
    AgentError,
    AgentExecution,
    AgentStatus,
    ConversationalInput,
    ConversationalResponse,
    SupervisorOutput,
)
from schemas.state import MASState
from core.logging_config import agent_span, get_logger

logger = get_logger(__name__)

AGENT_NAME = "conversational_agent"
PLATFORM = "gcp"


# ─────────────────────────────────────────────────────────────────────────────
# Dialogflow CX client
# ─────────────────────────────────────────────────────────────────────────────


def _detect_intent_dialogflow(
    session_id: str,
    utterance: str,
    language_code: str,
    project_id: str,
    location: str,
    agent_id: str,
) -> Dict[str, Any]:
    """
    Send a user utterance to a Dialogflow CX agent and return the raw
    detect-intent response.

    Args:
        session_id:    Unique session identifier for conversation continuity.
        utterance:     User's natural language input.
        language_code: BCP-47 language tag (e.g., "en-US").
        project_id:    GCP project ID.
        location:      Dialogflow CX location (e.g., "us-central1" or "global").
        agent_id:      Dialogflow CX agent UUID.

    Returns:
        Dict containing ``fulfillment_text``, ``intent``, ``confidence``,
        and ``parameters`` keys extracted from the Dialogflow response.

    Raises:
        RuntimeError: On API errors or missing dependencies.
    """
    try:
        from google.cloud.dialogflowcx_v3 import SessionsClient, TextInput, QueryInput
        from google.cloud.dialogflowcx_v3.types import session as dfcx_session

        client = SessionsClient(
            client_options={"api_endpoint": f"{location}-dialogflow.googleapis.com"}
        )

        session_path = client.session_path(project_id, location, agent_id, session_id)

        text_input = TextInput(text=utterance)
        query_input = QueryInput(text=text_input, language_code=language_code)
        request = dfcx_session.DetectIntentRequest(
            session=session_path,
            query_input=query_input,
        )

        response = client.detect_intent(request=request)
        qr = response.query_result
        intent = qr.match.intent.display_name if qr.match.intent else "Default Fallback Intent"

        messages = [m.text.text[0] for m in qr.response_messages if m.text.text]
        fulfillment_text = messages[0] if messages else "I'm sorry, I couldn't process that."

        return {
            "fulfillment_text": fulfillment_text,
            "intent": intent,
            "confidence": qr.match.confidence,
            "parameters": dict(qr.parameters),
            "end_interaction": qr.match.match_type.name == "END_SESSION",
        }

    except ImportError:
        raise RuntimeError(
            "google-cloud-dialogflow-cx package is not installed. "
            "Run: pip install google-cloud-dialogflow-cx"
        )


def _fallback_gemini_response(
    utterance: str,
    context: Dict[str, Any],
    supervisor_summary: str,
    model_id: str = "gemini-2.5-flash",
) -> str:
    """
    Generate a fallback response via Vertex AI Gemini when Dialogflow does not
    handle the utterance (low-confidence or fallback intent).

    Args:
        utterance:          User's question.
        context:            Conversation context dict (from state).
        supervisor_summary: The executive summary from the Supervisor agent.
        model_id:           Gemini model ID for the response.

    Returns:
        Plain-text response string.
    """
    try:
        import google.generativeai as genai

        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            genai.configure(api_key=api_key)

        model = genai.GenerativeModel(model_name=model_id)

        prompt = (
            f"You are a helpful compliance assistant. Answer the user's question "
            f"based on this risk assessment summary:\n\n{supervisor_summary}\n\n"
            f"User question: {utterance}\n\n"
            f"Provide a concise, accurate answer in 2-3 sentences. "
            f"Do not reveal internal model details or exact thresholds."
        )

        response = model.generate_content(prompt)
        return response.text.strip()

    except Exception as exc:
        logger.warning("conversational_agent.gemini_fallback_failed", error=str(exc))
        return (
            "I can see your risk assessment has been completed. "
            "Please refer to the email notification for detailed recommendations."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Salesforce case update
# ─────────────────────────────────────────────────────────────────────────────


def _update_salesforce_case(
    user_id: str,
    session_id: str,
    utterance: str,
    response: str,
    risk_level: str,
) -> None:
    """
    Log a conversation turn to Salesforce as a case activity.

    Args:
        user_id:    MAS user identifier.
        session_id: Conversation session ID.
        utterance:  User's input text.
        response:   Agent's response text.
        risk_level: Current risk level string for case categorisation.

    Side effects:
        Makes a Salesforce REST API call to create or update a Case record.
        Silently logs and continues on failure (non-critical path).
    """
    try:
        from integrations.salesforce_client import SalesforceClient

        sf = SalesforceClient()
        sf.log_conversation_activity(
            user_id=user_id,
            session_id=session_id,
            utterance=utterance,
            response=response,
            risk_level=risk_level,
        )
    except Exception as exc:
        logger.warning(
            "conversational_agent.salesforce_update_failed",
            error=str(exc),
        )


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node
# ─────────────────────────────────────────────────────────────────────────────


async def run(state: MASState) -> Dict[str, Any]:
    """
    LangGraph node function for the Conversational Agent.

    Routes the user's utterance through Dialogflow CX.  If Dialogflow returns
    a low-confidence or fallback intent, escalates to Gemini for a direct
    answer grounded in the supervisor summary.  Logs the interaction to
    Salesforce as a non-critical side effect.

    Args:
        state: Current MASState containing ``session_id``, ``raw_input``,
               and optionally ``supervisor_output``.

    Returns:
        Partial MASState dict with ``conversational_resp``,
        ``execution_history``, ``errors``, and ``execution_times``.
    """
    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc)

    with agent_span(logger, AGENT_NAME, state["request_id"]) as span:
        try:
            utterance = state["raw_input"].get("utterance", "")
            if not utterance:
                raise ValueError("raw_input.utterance is required for ConversationalAgent")

            conv_input = ConversationalInput(
                session_id=state["session_id"],
                user_utterance=utterance,
                context=state.get("metadata", {}),
                language_code=state["raw_input"].get("language_code", "en-US"),
            )

            project_id = os.environ.get("GCP_PROJECT_ID", "")
            location = os.environ.get("DIALOGFLOW_LOCATION", "us-central1")
            agent_id = os.environ.get("DIALOGFLOW_AGENT_ID", "")

            df_result = _detect_intent_dialogflow(
                session_id=conv_input.session_id,
                utterance=conv_input.user_utterance,
                language_code=conv_input.language_code,
                project_id=project_id,
                location=location,
                agent_id=agent_id,
            )

            # Escalate to Gemini if Dialogflow confidence is low
            CONFIDENCE_THRESHOLD = 0.5
            if df_result["confidence"] < CONFIDENCE_THRESHOLD:
                supervisor: Optional[SupervisorOutput] = state.get("supervisor_output")
                summary = (
                    supervisor.executive_summary
                    if supervisor
                    else "Risk assessment has been completed."
                )
                fulfillment_text = _fallback_gemini_response(
                    utterance=conv_input.user_utterance,
                    context=conv_input.context,
                    supervisor_summary=summary,
                )
                intent = "gemini_fallback"
            else:
                fulfillment_text = df_result["fulfillment_text"]
                intent = df_result["intent"]

            response = ConversationalResponse(
                session_id=conv_input.session_id,
                fulfillment_text=fulfillment_text,
                intent_detected=intent,
                confidence=df_result["confidence"],
                parameters=df_result.get("parameters", {}),
                end_interaction=df_result.get("end_interaction", False),
            )

            analysis_result = state.get("analysis_result")
            risk_level_str = (
                analysis_result.risk_level.value if analysis_result else "unknown"
            )
            _update_salesforce_case(
                user_id=state["user_id"],
                session_id=conv_input.session_id,
                utterance=conv_input.user_utterance,
                response=fulfillment_text,
                risk_level=risk_level_str,
            )

            duration_ms = (time.perf_counter() - start_time) * 1000
            span["intent"] = intent
            span["confidence"] = df_result["confidence"]

            return {
                "conversational_resp": response,
                "current_agent": AGENT_NAME,
                "execution_history": [
                    AgentExecution(
                        agent_name=AGENT_NAME,
                        status=AgentStatus.SUCCESS,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        duration_ms=round(duration_ms, 2),
                        output_summary={"intent": intent, "confidence": df_result["confidence"]},
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
                "conversational_resp": ConversationalResponse(
                    session_id=state.get("session_id", "unknown"),
                    fulfillment_text="I'm sorry, I'm temporarily unavailable. Please try again later.",
                    intent_detected="system_error",
                    confidence=0.0,
                    end_interaction=True,
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
