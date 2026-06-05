"""
LangGraph StateGraph workflow definition for the distributed MAS runtime.

The orchestrator and policy gates remain on-prem, while the agent nodes can be
invoked either:
  - remotely over HTTP as independent services (default, production), or
  - locally in-process for unit testing and debugging.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import httpx
from langgraph.graph import END, StateGraph

from agents import orchestrator, policy_gate
from agents import analyst as local_analyst
from agents import conversational_agent as local_conversational_agent
from agents import data_fetcher as local_data_fetcher
from agents import data_validator as local_data_validator
from agents import email_agent as local_email_agent
from agents import supervisor as local_supervisor
from control_tower.config_manager import get_settings
from core.remote_agent import RemoteAgentConfig, RemoteAgentInvoker
from schemas.state import MASState

NodeFn = Callable[[MASState], Any]


def _build_remote_nodes(
    *,
    base_url_overrides: Optional[Dict[str, str]] = None,
    transport_overrides: Optional[Dict[str, httpx.AsyncBaseTransport]] = None,
) -> Dict[str, NodeFn]:
    """
    Build workflow node callables that invoke deployed agent services over HTTP.

    Args:
        base_url_overrides: Optional per-agent URL overrides used in tests.
        transport_overrides: Optional per-agent HTTPX transports used for ASGI tests.

    Returns:
        Mapping of graph node names to async callables.
    """
    settings = get_settings()
    url_map = {
        "data_fetcher": settings.data_fetcher_url,
        "data_validator": settings.data_validator_url,
        "analyst": settings.analyst_url,
        "supervisor": settings.supervisor_url,
        "email_agent": settings.email_agent_url,
        "conversational_agent": settings.conversational_agent_url,
    }
    if base_url_overrides:
        url_map.update(base_url_overrides)

    transport_map = transport_overrides or {}
    timeout = settings.service_request_timeout_seconds

    invokers = {
        "data_fetcher": RemoteAgentInvoker(
            RemoteAgentConfig(
                agent_name="data_fetcher",
                platform="aws",
                service_url=url_map["data_fetcher"],
                timeout_seconds=timeout,
            ),
            transport=transport_map.get("data_fetcher"),
        ),
        "data_validator": RemoteAgentInvoker(
            RemoteAgentConfig(
                agent_name="data_validator",
                platform="on-prem",
                service_url=url_map["data_validator"],
                timeout_seconds=timeout,
            ),
            transport=transport_map.get("data_validator"),
        ),
        "analyst": RemoteAgentInvoker(
            RemoteAgentConfig(
                agent_name="analyst",
                platform="aws",
                service_url=url_map["analyst"],
                timeout_seconds=timeout,
            ),
            transport=transport_map.get("analyst"),
        ),
        "supervisor": RemoteAgentInvoker(
            RemoteAgentConfig(
                agent_name="supervisor",
                platform="on-prem",
                service_url=url_map["supervisor"],
                timeout_seconds=timeout,
            ),
            transport=transport_map.get("supervisor"),
        ),
        "email_agent": RemoteAgentInvoker(
            RemoteAgentConfig(
                agent_name="email_agent",
                platform="azure",
                service_url=url_map["email_agent"],
                timeout_seconds=timeout,
            ),
            transport=transport_map.get("email_agent"),
        ),
        "conversational_agent": RemoteAgentInvoker(
            RemoteAgentConfig(
                agent_name="conversational_agent",
                platform="gcp",
                service_url=url_map["conversational_agent"],
                timeout_seconds=timeout,
            ),
            transport=transport_map.get("conversational_agent"),
        ),
    }

    return {
        agent_name: invoker.invoke
        for agent_name, invoker in invokers.items()
    }


def _build_local_nodes() -> Dict[str, NodeFn]:
    """Build workflow nodes that call the agent modules directly in-process."""
    return {
        "data_fetcher": local_data_fetcher.run,
        "data_validator": local_data_validator.run,
        "analyst": local_analyst.run,
        "supervisor": local_supervisor.run,
        "email_agent": local_email_agent.run,
        "conversational_agent": local_conversational_agent.run,
    }


def build_workflow(
    *,
    execution_mode: Optional[str] = None,
    base_url_overrides: Optional[Dict[str, str]] = None,
    transport_overrides: Optional[Dict[str, httpx.AsyncBaseTransport]] = None,
) -> Any:
    """
    Construct and compile the MAS LangGraph StateGraph.

    Args:
        execution_mode: `remote` for HTTP-based distributed agents or `local`
            for in-process execution. Defaults to settings.agent_execution_mode.
        base_url_overrides: Optional per-agent URL overrides for tests.
        transport_overrides: Optional per-agent HTTPX transports for ASGI tests.

    Returns:
        Compiled LangGraph workflow ready to `await graph.ainvoke(state)`.
    """
    settings = get_settings()
    mode = (execution_mode or settings.agent_execution_mode).strip().lower()
    if mode not in {"local", "remote"}:
        raise ValueError(f"Unsupported agent execution mode: {mode}")

    agent_nodes = (
        _build_remote_nodes(
            base_url_overrides=base_url_overrides,
            transport_overrides=transport_overrides,
        )
        if mode == "remote"
        else _build_local_nodes()
    )

    graph = StateGraph(MASState)

    graph.add_node("orchestrator", orchestrator.run)
    graph.add_node("data_fetcher", agent_nodes["data_fetcher"])
    graph.add_node("data_validator", agent_nodes["data_validator"])
    graph.add_node("policy_gate_pre", policy_gate.pre_analysis_run)
    graph.add_node("analyst", agent_nodes["analyst"])
    graph.add_node("policy_gate_post", policy_gate.post_analysis_run)
    graph.add_node("supervisor", agent_nodes["supervisor"])
    graph.add_node("email_agent", agent_nodes["email_agent"])
    graph.add_node("conversational_agent", agent_nodes["conversational_agent"])
    graph.add_node("finalize", orchestrator.finalize)
    graph.add_node("error_handler", orchestrator.error_handler)

    graph.set_entry_point("orchestrator")
    graph.add_edge("orchestrator", "data_fetcher")
    graph.add_edge("data_fetcher", "data_validator")
    graph.add_edge("data_validator", "policy_gate_pre")
    graph.add_edge("analyst", "policy_gate_post")

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

    graph.add_edge("conversational_agent", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("error_handler", END)

    return graph.compile()


compiled_graph = build_workflow()
