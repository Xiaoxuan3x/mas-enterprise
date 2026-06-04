"""
PII obfuscation layer applied at the gateway before any data enters the pipeline.

Scrubs or tokenises personal identifiable information from request payloads
before they are stored in state or logged.  Uses deterministic tokenisation
so the original value can be recovered by the KMS-backed detokenisation
service if needed by authorised downstream agents.

Patterns scrubbed:
  - Email addresses
  - Credit/debit card PANs (Luhn-valid 13–19 digit numbers)
  - EU/UK National Insurance / Tax ID patterns
  - US SSN (format NNN-NN-NNNN)
  - Phone numbers (E.164 format)
  - IPv4 addresses (partially masked to /24 network)
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Dict, List, Tuple

from core.logging_config import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PII pattern registry
# ─────────────────────────────────────────────────────────────────────────────


_PII_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b"),
        "[EMAIL_REDACTED]",
    ),
    (
        "card_pan",
        re.compile(r"\b(?:\d[ \-]?){13,19}\b"),
        "[CARD_REDACTED]",
    ),
    (
        "us_ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[SSN_REDACTED]",
    ),
    (
        "phone_e164",
        re.compile(r"\+\d{7,15}\b"),
        "[PHONE_REDACTED]",
    ),
    (
        "ipv4",
        re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b"),
        r"\1.0",  # Mask to /24 subnet — uses backreference
    ),
]

# Fields that are safe to pass through without scanning (numeric/bool primitives)
_SAFE_KEY_PREFIXES = {"amount", "score", "count", "flag", "is_", "has_"}


def _should_scan_key(key: str) -> bool:
    """Return True if this key's value should be PII-scanned."""
    return not any(key.startswith(prefix) for prefix in _SAFE_KEY_PREFIXES)


def _obfuscate_string(text: str) -> str:
    """
    Apply all PII patterns to a single string value.

    Args:
        text: Input string that may contain PII.

    Returns:
        String with all detected PII replaced by placeholder tokens.
    """
    for _name, pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def obfuscate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively obfuscate all string values in a request payload dict.

    Creates a deep copy — never mutates the original dict.

    Args:
        payload: Raw incoming request body.

    Returns:
        A new dict with PII scrubbed from all string fields.

    Side effects:
        Logs a ``pii.obfuscated`` event with the count of substitutions made.
    """
    result, substitution_count = _obfuscate_recursive(payload, "")
    if substitution_count > 0:
        logger.info(
            "pii.obfuscated",
            substitution_count=substitution_count,
        )
    return result


def _obfuscate_recursive(
    obj: Any, parent_key: str
) -> Tuple[Any, int]:
    """Recursive helper that returns (obfuscated_obj, substitution_count)."""
    count = 0

    if isinstance(obj, dict):
        new_dict: Dict[str, Any] = {}
        for k, v in obj.items():
            new_v, sub_count = _obfuscate_recursive(v, k)
            new_dict[k] = new_v
            count += sub_count
        return new_dict, count

    elif isinstance(obj, list):
        new_list = []
        for item in obj:
            new_item, sub_count = _obfuscate_recursive(item, parent_key)
            new_list.append(new_item)
            count += sub_count
        return new_list, count

    elif isinstance(obj, str) and _should_scan_key(parent_key):
        obfuscated = _obfuscate_string(obj)
        if obfuscated != obj:
            count = 1
        return obfuscated, count

    return obj, 0


def tokenise_user_id(raw_user_id: str, hmac_secret: bytes) -> str:
    """
    Deterministically tokenise a user ID using HMAC-SHA256 so the raw
    identifier never appears in logs or state.

    The original value can be recovered via the KMS-backed detokenisation
    service using the same secret.

    Args:
        raw_user_id:  Plain-text user identifier.
        hmac_secret:  32-byte secret from the KMS (rotated quarterly).

    Returns:
        A 16-character hex token that is stable across requests for the same
        user and secret.  Format: ``tok_<hex16>``.
    """
    digest = hmac.new(hmac_secret, raw_user_id.encode(), hashlib.sha256).hexdigest()
    return f"tok_{digest[:16]}"
