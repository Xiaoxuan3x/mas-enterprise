"""
MAS Enterprise — application entry point.

Starts the FastAPI gateway on the configured host/port and mounts the
Prometheus metrics endpoint on a separate port for scraping.

Usage:
    python main.py                              # production
    uvicorn main:app --reload --port 8000       # development with hot reload
"""
from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI
from fastapi.routing import Mount

from core.logging_config import configure_logging
from gateway.ingress import app as gateway_app
from observability.metrics import metrics_app
from control_tower.policy_engine import policy_engine

configure_logging(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    json_output=os.environ.get("LOG_FORMAT", "json") == "json",
)

# Load policies at startup so the first request doesn't pay the I/O cost
policy_engine.load_policies()

# Root app mounts gateway and metrics on separate paths
app = FastAPI(title="MAS Enterprise", version="1.0.0")
app.mount("/metrics", metrics_app)
app.mount("/", gateway_app)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        workers=int(os.environ.get("WORKERS", "4")),
        log_config=None,  # structlog handles logging
        access_log=False,
    )
