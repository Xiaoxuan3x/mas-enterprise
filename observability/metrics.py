"""
Prometheus metrics for the MAS pipeline.

Exposes a /metrics endpoint (via prometheus-client) for scraping by a
Prometheus server co-located with the on-prem NVIDIA deployment.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info, make_asgi_app

# ── Agent execution counters ──────────────────────────────────────────────────

agent_executions_total = Counter(
    "mas_agent_executions_total",
    "Total agent node invocations",
    ["agent_name", "status", "platform"],
)

agent_duration_seconds = Histogram(
    "mas_agent_duration_seconds",
    "Agent execution wall-clock time",
    ["agent_name", "platform"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

# ── LLM token usage ───────────────────────────────────────────────────────────

llm_tokens_total = Counter(
    "mas_llm_tokens_total",
    "Total LLM tokens consumed",
    ["agent_name", "token_type", "model_id"],
)

# ── Pipeline outcomes ─────────────────────────────────────────────────────────

pipeline_requests_total = Counter(
    "mas_pipeline_requests_total",
    "Total pipeline invocations",
    ["tenant_id", "status"],
)

pipeline_risk_level = Counter(
    "mas_pipeline_risk_level_total",
    "Pipeline completions by risk level",
    ["risk_level"],
)

pipeline_duration_ms = Histogram(
    "mas_pipeline_duration_ms",
    "End-to-end pipeline duration in milliseconds",
    buckets=[100, 250, 500, 1000, 2500, 5000, 10000, 30000],
)

# ── Gateway security events ───────────────────────────────────────────────────

security_events_total = Counter(
    "mas_security_events_total",
    "Security-related gateway events",
    ["event_type"],  # jwt_rejected, injection_detected, rate_limit_exceeded
)

# ── Service info ──────────────────────────────────────────────────────────────

service_info = Info("mas_service", "MAS Enterprise service metadata")
service_info.info({"version": "1.0.0", "platform": "multi-cloud"})


def record_agent_execution(
    agent_name: str,
    status: str,
    platform: str,
    duration_seconds: float,
) -> None:
    """
    Record a single agent execution in Prometheus metrics.

    Args:
        agent_name:       Name of the agent node.
        status:           Execution status string ("success", "failure").
        platform:         Deployment platform ("on-prem", "aws", "azure", "gcp").
        duration_seconds: Wall-clock duration in seconds.
    """
    agent_executions_total.labels(
        agent_name=agent_name, status=status, platform=platform
    ).inc()
    agent_duration_seconds.labels(
        agent_name=agent_name, platform=platform
    ).observe(duration_seconds)


def record_token_usage(
    agent_name: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """
    Record LLM token consumption in Prometheus counters.

    Args:
        agent_name:        LLM-backed agent name.
        model_id:          Fully-qualified model identifier.
        prompt_tokens:     Prompt token count.
        completion_tokens: Completion token count.
    """
    llm_tokens_total.labels(
        agent_name=agent_name, token_type="prompt", model_id=model_id
    ).inc(prompt_tokens)
    llm_tokens_total.labels(
        agent_name=agent_name, token_type="completion", model_id=model_id
    ).inc(completion_tokens)


# Mount this on a separate FastAPI app for Prometheus scraping
metrics_app = make_asgi_app()
