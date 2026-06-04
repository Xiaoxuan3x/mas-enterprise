"""
Structured JSON logging configuration for the Multi-Agent System.

Every agent transition, execution time, and LLM token usage is emitted as
a structured JSON event.  Events are compatible with Observe's OTEL ingest
endpoint and include trace context for distributed tracing.

Side effects:
    Calling ``configure_logging()`` replaces the root logging handler with a
    structlog JSON renderer bound to stdout.  Call once at process startup.
"""
from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

import structlog
from structlog.types import EventDict, WrappedLogger


def _add_service_context(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Inject static service metadata into every log record."""
    event_dict.setdefault("service", "mas-enterprise")
    event_dict.setdefault("version", "1.0.0")
    return event_dict


def _add_timestamp(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Inject ISO-8601 UTC timestamp (structlog's built-in uses local time)."""
    from datetime import datetime, timezone

    event_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """
    Configure structlog for structured JSON output compatible with Observe.

    Args:
        level:       Python logging level string (e.g., "INFO", "DEBUG").
        json_output: When False, renders human-readable output for local dev.

    Side effects:
        Replaces the root ``logging.Logger`` handler and configures structlog's
        global pipeline.  Should be called exactly once at process startup.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_service_context,
        _add_timestamp,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging through structlog so third-party libs are captured
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Return a structlog logger bound to the given module/agent name.

    Args:
        name: Typically ``__name__`` of the calling module.

    Returns:
        A BoundLogger that emits structured JSON events.
    """
    return structlog.get_logger(name)


@contextmanager
def agent_span(
    logger: structlog.BoundLogger,
    agent_name: str,
    request_id: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """
    Context manager that emits structured start/finish log events and measures
    wall-clock duration for a single agent execution span.

    Args:
        logger:     Bound structlog logger.
        agent_name: Name of the agent node being executed.
        request_id: End-to-end correlation ID.
        extra:      Additional fields merged into log events.

    Yields:
        A mutable dict that callers can populate with output metadata to be
        included in the finish event.

    Side effects:
        Binds ``agent_name`` and ``request_id`` to the structlog context
        for the duration of the ``with`` block, then clears them.
    """
    span_data: Dict[str, Any] = {}
    ctx = {"agent_name": agent_name, "request_id": request_id, **(extra or {})}
    structlog.contextvars.bind_contextvars(**ctx)

    logger.info("agent.start", agent=agent_name)
    start = time.perf_counter()
    try:
        yield span_data
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "agent.finish",
            agent=agent_name,
            duration_ms=round(duration_ms, 2),
            **span_data,
        )
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.error(
            "agent.error",
            agent=agent_name,
            duration_ms=round(duration_ms, 2),
            error=str(exc),
            exc_info=True,
        )
        raise
    finally:
        structlog.contextvars.unbind_contextvars(*ctx.keys())


def log_token_usage(
    logger: structlog.BoundLogger,
    agent_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    model_id: str,
) -> None:
    """
    Emit a structured token-usage event for LLM-backed agents.

    Args:
        logger:            Bound logger for the calling agent.
        agent_name:        Name of the LLM-backed agent.
        prompt_tokens:     Tokens consumed by the prompt.
        completion_tokens: Tokens produced in the completion.
        model_id:          Fully-qualified model identifier string.
    """
    logger.info(
        "agent.token_usage",
        agent=agent_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        model_id=model_id,
    )
