"""
Prompt injection detection and blocking for the MAS API gateway.

Scans all free-text fields in incoming requests for known prompt injection
patterns before the payload reaches any LLM-backed agent.  Detection is
purely regex/heuristic-based — no LLM is used for the guard itself to avoid
a bootstrap trust problem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class InjectionMatch:
    """Describes a single detected injection pattern."""

    field: str
    pattern_name: str
    matched_text: str


class PromptInjectionError(Exception):
    """Raised when one or more injection patterns are detected in a request."""

    def __init__(self, matches: List[InjectionMatch]) -> None:
        self.matches = matches
        super().__init__(
            f"Prompt injection detected in {len(matches)} field(s): "
            + ", ".join(m.field for m in matches)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Injection pattern registry
# ─────────────────────────────────────────────────────────────────────────────

_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("ignore_previous", re.compile(r"ignore\s+(?:previous|all)\s+instructions?", re.IGNORECASE)),
    ("system_override", re.compile(r"(?:system|assistant):\s*(?:you are|act as|pretend)", re.IGNORECASE)),
    ("jailbreak_dan", re.compile(r"\bDAN\b|\bdo anything now\b", re.IGNORECASE)),
    ("role_override", re.compile(r"you are now\s+(?:an?\s+)?(?:evil|unrestricted|jailbroken|unfiltered)", re.IGNORECASE)),
    ("instruction_delimiter", re.compile(r"<\|(?:system|user|assistant|im_start|im_end)\|>")),
    ("prompt_leak", re.compile(r"repeat\s+(?:everything|your\s+instructions?|the\s+system\s+prompt)", re.IGNORECASE)),
    ("xml_injection", re.compile(r"<\s*(?:system|instruction|override)\s*>", re.IGNORECASE)),
    ("base64_encoded_instruction", re.compile(r"base64[_\s]*decode", re.IGNORECASE)),
    ("eval_injection", re.compile(r"(?:eval|exec)\s*\(", re.IGNORECASE)),
    ("token_boundary", re.compile(r"###\s*(?:Instruction|System|Input):", re.IGNORECASE)),
]

# Fields within the request body that may contain free text and should be scanned
_TEXT_FIELDS = {"utterance", "comment", "description", "notes", "query", "message"}


def scan_payload(payload: Dict[str, Any]) -> List[InjectionMatch]:
    """
    Recursively scan a request payload dict for prompt injection patterns.

    Inspects all string values in fields listed in ``_TEXT_FIELDS`` at any
    nesting depth.  Numeric and boolean fields are skipped.

    Args:
        payload: The raw request body dict from the FastAPI endpoint.

    Returns:
        List of InjectionMatch records for every detected pattern.
        Empty list means the payload is clean.
    """
    matches: List[InjectionMatch] = []
    _scan_recursive(payload, parent_key="", matches=matches)
    return matches


def _scan_recursive(
    obj: Any,
    parent_key: str,
    matches: List[InjectionMatch],
) -> None:
    """Recurse into dicts and lists to find all scannable string fields."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            full_key = f"{parent_key}.{key}" if parent_key else key
            _scan_recursive(value, full_key, matches)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _scan_recursive(item, f"{parent_key}[{idx}]", matches)
    elif isinstance(obj, str):
        # Only scan fields whose terminal name is in the allow-list
        terminal = parent_key.split(".")[-1].rstrip("]").split("[")[0]
        if terminal in _TEXT_FIELDS:
            _scan_string(obj, parent_key, matches)


def _scan_string(
    text: str,
    field: str,
    matches: List[InjectionMatch],
) -> None:
    """Run all registered patterns against a single text value."""
    for pattern_name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            snippet = text[max(0, match.start() - 10): match.end() + 10]
            matches.append(
                InjectionMatch(
                    field=field,
                    pattern_name=pattern_name,
                    matched_text=f"...{snippet}...",
                )
            )


def enforce_no_injection(payload: Dict[str, Any], request_id: str) -> None:
    """
    Scan payload and raise ``PromptInjectionError`` if any pattern matches.

    Intended to be called at the gateway before state initialisation so that
    malicious input never reaches an LLM-backed agent.

    Args:
        payload:    Incoming request body dict.
        request_id: Correlation ID for logging.

    Raises:
        PromptInjectionError: If one or more injection patterns are detected.

    Side effects:
        Logs a ``security.injection_detected`` event with pattern details.
    """
    matches = scan_payload(payload)
    if matches:
        logger.warning(
            "security.injection_detected",
            request_id=request_id,
            match_count=len(matches),
            fields=[m.field for m in matches],
            patterns=[m.pattern_name for m in matches],
        )
        raise PromptInjectionError(matches)
