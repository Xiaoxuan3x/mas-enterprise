"""
ASGI applications for each independently deployable MAS agent service.

The repository keeps the agent logic and the service wrappers in one codebase,
but each `*_app` object below can be deployed as its own container or process.
"""
from __future__ import annotations

from fastapi import FastAPI

from agents import analyst, conversational_agent, data_fetcher, data_validator, email_agent, supervisor
from services.runtime import create_agent_service_app

data_fetcher_app = create_agent_service_app(
    agent_name="data_fetcher",
    platform="aws",
    runner=data_fetcher.run,
)

data_validator_app = create_agent_service_app(
    agent_name="data_validator",
    platform="on-prem",
    runner=data_validator.run,
)

analyst_app = create_agent_service_app(
    agent_name="analyst",
    platform="aws",
    runner=analyst.run,
)

supervisor_app = create_agent_service_app(
    agent_name="supervisor",
    platform="on-prem",
    runner=supervisor.run,
)

email_agent_app = create_agent_service_app(
    agent_name="email_agent",
    platform="azure",
    runner=email_agent.run,
)

conversational_agent_app = create_agent_service_app(
    agent_name="conversational_agent",
    platform="gcp",
    runner=conversational_agent.run,
)

# Composite app used for local testing or sidecar-style deployments.
agent_mesh_app = FastAPI(title="MAS Agent Mesh", version="1.0.0")
agent_mesh_app.mount("/data-fetcher", data_fetcher_app)
agent_mesh_app.mount("/data-validator", data_validator_app)
agent_mesh_app.mount("/analyst", analyst_app)
agent_mesh_app.mount("/supervisor", supervisor_app)
agent_mesh_app.mount("/email-agent", email_agent_app)
agent_mesh_app.mount("/conversational-agent", conversational_agent_app)
