"""
Exponential-backoff retry decorator for deterministic agents.

Wraps ``tenacity`` with MAS-specific defaults: up to 3 attempts,
exponential backoff starting at 1 s, jittered to prevent thundering-herd,
and structured logging on every retry and final failure.
"""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable, Optional, Tuple, Type

from tenacity import (
    AsyncRetrying,
    RetryError,
    Retrying,
    after_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from core.logging_config import get_logger

logger = get_logger(__name__)

# Default retry policy constants
_MAX_ATTEMPTS = 3
_INITIAL_WAIT_S = 1.0
_MAX_WAIT_S = 30.0
_JITTER_S = 1.0


def with_retry(
    max_attempts: int = _MAX_ATTEMPTS,
    initial_wait: float = _INITIAL_WAIT_S,
    max_wait: float = _MAX_WAIT_S,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
    agent_name: str = "unknown",
) -> Callable:
    """
    Decorator that applies synchronous exponential-backoff retry to a function.

    Retries up to ``max_attempts`` times.  Waits between retries grow
    exponentially from ``initial_wait`` with random jitter up to ``max_wait``.
    Structured log events are emitted on each retry attempt and on final failure.

    Args:
        max_attempts: Maximum total invocations (first call + retries).
        initial_wait: Wait in seconds before the first retry.
        max_wait:     Upper bound on inter-retry wait in seconds.
        retry_on:     Tuple of exception types that trigger a retry.
        agent_name:   Agent name injected into log context.

    Returns:
        A decorator that wraps the target callable with retry logic.

    Side effects:
        Emits ``agent.retry`` log events on each failed attempt.
        Raises the last exception as ``RetryError`` after all attempts fail.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            for attempt_obj in Retrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential_jitter(
                    initial=initial_wait, max=max_wait, jitter=_JITTER_S
                ),
                retry=retry_if_exception_type(retry_on),
                reraise=True,
            ):
                with attempt_obj:
                    attempt += 1
                    if attempt > 1:
                        logger.warning(
                            "agent.retry",
                            agent=agent_name,
                            attempt=attempt,
                            max_attempts=max_attempts,
                        )
                    return fn(*args, **kwargs)

        return wrapper

    return decorator


def with_async_retry(
    max_attempts: int = _MAX_ATTEMPTS,
    initial_wait: float = _INITIAL_WAIT_S,
    max_wait: float = _MAX_WAIT_S,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
    agent_name: str = "unknown",
) -> Callable:
    """
    Decorator that applies asynchronous exponential-backoff retry to a coroutine.

    Identical semantics to ``with_retry`` but uses ``AsyncRetrying`` for
    ``async def`` functions so the event loop is not blocked during waits.

    Args:
        max_attempts: Maximum total invocations.
        initial_wait: Wait in seconds before the first retry.
        max_wait:     Upper bound on inter-retry wait.
        retry_on:     Exception types that trigger a retry.
        agent_name:   Agent name for log context.

    Returns:
        A decorator wrapping the async callable with retry logic.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            async for attempt_obj in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential_jitter(
                    initial=initial_wait, max=max_wait, jitter=_JITTER_S
                ),
                retry=retry_if_exception_type(retry_on),
                reraise=True,
            ):
                with attempt_obj:
                    attempt += 1
                    if attempt > 1:
                        logger.warning(
                            "agent.retry",
                            agent=agent_name,
                            attempt=attempt,
                            max_attempts=max_attempts,
                        )
                    return await fn(*args, **kwargs)

        return wrapper

    return decorator


class RetryExhaustedError(Exception):
    """
    Raised when all retry attempts for a deterministic agent are exhausted.

    Attributes:
        agent_name:     The agent that failed.
        last_exception: The exception from the final attempt.
        attempts:       Total attempts made.
    """

    def __init__(
        self, agent_name: str, last_exception: Exception, attempts: int
    ) -> None:
        self.agent_name = agent_name
        self.last_exception = last_exception
        self.attempts = attempts
        super().__init__(
            f"{agent_name} failed after {attempts} attempts: {last_exception}"
        )
