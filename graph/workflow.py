"""
LangGraph StateGraph workflow definition.

Wires all agent nodes and routing functions into a compiled graph.
The graph is the single entry point for executing the full MAS pipeline.

Data flow:
  START
    → orchestrator          (on-prem: state init, routing)
    → data_fetcher          (AWS: DynamoDB/API data retrieval)
    → data_validator        (on-prem: deterministic rule checks)
    ↓ conditional routing (route_after_validation)
    → analyst               (AWS: fraud scoring + backtesting)    OR
    → supervisor            (on-prem: Gemini summary + recs)      ← also after analyst
    ↓ conditional routing (route_after_supervisor)
    → email_agent           (Azure: notification dispatch)        ← optional
    ↓ conditional routing (route_after_email)
    → conversational_agent  (GCP: Dialogflow CX + Gemini fallback) ← optional
    → finalize / error_handler
  END
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from agents import orchestrator, data_fetcher, data_validator, analyst, supervisor
from agents import policy_gate
from agents import email_agent, conversational_agent
from schemas.state import MASState


def build_workflow() -> Any:
    """
    Construct and compile the MAS LangGraph StateGraph.

    Each agent is added as a node; conditional edges implement the routing
    decisions described in the module docstring.

    Returns:
        A compiled LangGraph ``CompiledGraph`` ready to invoke with
        ``await graph.ainvoke(initial_state)``.

    Side effects:
        Calls ``StateGraph.compile()`` which validates graph structure
        (no orphan nodes, no unreachable terminals) at startup.
    """
    graph = StateGraph(MASState)

    # ── Register agent nodes ─────────────────────────────────────────────
    graph.add_node("orchestrator", orchestrator.run)
    graph.add_node("data_fetcher", data_fetcher.run)
    graph.add_node("data_validator", data_validator.run)
    graph.add_node("policy_gate_pre", policy_gate.pre_analysis_run)
    graph.add_node("analyst", analyst.run)
    graph.add_node("policy_gate_post", policy_gate.post_analysis_run)
    graph.add_node("supervisor", supervisor.run)
    graph.add_node("email_agent", email_agent.run)
    graph.add_node("conversational_agent", conversational_agent.run)
    graph.add_node("finalize", orchestrator.finalize)
    graph.add_node("error_handler", orchestrator.error_handler)

    # ── Entry point ──────────────────────────────────────────────────────
    graph.set_entry_point("orchestrator")

    # ── Deterministic edges ──────────────────────────────────────────────
    graph.add_edge("orchestrator", "data_fetcher")
    graph.add_edge("data_fetcher", "data_validator")
    graph.add_edge("data_validator", "policy_gate_pre")
    graph.add_edge("analyst", "policy_gate_post")

    # ── Conditional edges ────────────────────────────────────────────────
    graph.add_conditional_edges(
        "policy_gate_pre",
        orchestrator.route_after_validation,
        {
            "analyst": "analyst",
            "supervisor": "supervisor",
            "error_handler": "error_handler",
        },
    )

    graph.add_conditional_edges(
        "policy_gate_post",
        orchestrator.route_after_post_analysis_policy,
        {
            "supervisor": "supervisor",
            "error_handler": "error_handler",
        },
    )

    graph.add_conditional_edges(
        "supervisor",
        orchestrator.route_after_supervisor,
        {
            "email_agent": "email_agent",
            "conversational_agent": "conversational_agent",
            "finalize": "finalize",
        },
    )

    graph.add_conditional_edges(
        "email_agent",
        orchestrator.route_after_email,
        {
            "conversational_agent": "conversational_agent",
            "finalize": "finalize",
        },
    )

    # ── Terminal edges ───────────────────────────────────────────────────
    graph.add_edge("conversational_agent", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("error_handler", END)

    return graph.compile()


# Module-level compiled graph — import and call directly in production
compiled_graph = build_workflow()
