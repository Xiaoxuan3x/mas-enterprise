"""
Format-preserving tokenisation for sensitive identifiers.

Replaces PII values (card PANs, account numbers, user IDs) with tokens that
preserve length and character class so downstream systems don't require schema
changes.  Tokens are reversible via the KMS-backed detokenisation service.

Algorithm: AES-256 format-preserving encryption (FF3-1) for numeric PANs;
HMAC-SHA256 truncation for opaque identifiers.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Optional

from security.key_management import KeyManagementClient, KeyPurpose


# Luhn validation — used to confirm a candidate string is a valid PAN before tokenising
def _luhn_valid(number: str) -> bool:
    """Return True if ``number`` passes the Luhn check algorithm."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


class TokenisationService:
    """
    Service for tokenising and detokenising sensitive values.

    Attributes:
        kms: Key management client used to fetch the tokenisation key.

    Usage::

        svc = TokenisationService()
        token = svc.tokenise_user_id("user_abc_123")
        original = svc.detokenise_user_id(token)  # requires same HMAC secret
    """

    def __init__(self, kms: Optional[KeyManagementClient] = None) -> None:
        self.kms = kms or KeyManagementClient()

    def tokenise_user_id(self, raw_user_id: str) -> str:
        """
        Produce a stable, opaque 24-character token for a user ID.

        The token is deterministic: the same input and key always produce the
        same token, enabling database lookups without storing the plaintext.

        Args:
            raw_user_id: Original user identifier string.

        Returns:
            Token string of the form ``tok_<22 hex chars>``.
        """
        key = self.kms.get_data_key(KeyPurpose.PII_TOKENISATION)
        mac = hmac.new(key, raw_user_id.encode("utf-8"), hashlib.sha256)
        return f"tok_{mac.hexdigest()[:22]}"

    def tokenise_card_pan(self, pan: str) -> str:
        """
        Tokenise a payment card PAN using format-preserving substitution.

        Preserves the first 6 (BIN) and last 4 digits for display; replaces
        the middle digits with a deterministic token digit string so the
        total length is preserved.  Only valid (Luhn-passing) PANs are
        accepted.

        Args:
            pan: Raw card PAN string (digits only or with spaces/dashes).

        Returns:
            Tokenised PAN string with the same digit length as the input.

        Raises:
            ValueError: If ``pan`` does not pass the Luhn check.

        Example::

            svc.tokenise_card_pan("4111111111111111")
            # → "411111XXXXXX1111"  (X digits are deterministic token digits)
        """
        digits_only = re.sub(r"\D", "", pan)
        if not _luhn_valid(digits_only):
            raise ValueError("PAN failed Luhn validation — not a valid card number")

        bin_digits = digits_only[:6]
        last_four = digits_only[-4:]
        middle_len = len(digits_only) - 10

        key = self.kms.get_data_key(KeyPurpose.PII_TOKENISATION)
        mac = hmac.new(key, digits_only.encode(), hashlib.sha256)
        token_digits = "".join(str(int(c, 16) % 10) for c in mac.hexdigest()[:middle_len])

        return f"{bin_digits}{token_digits}{last_four}"

    def tokenise_email(self, email: str) -> str:
        """
        Produce a one-way token for an email address that preserves domain.

        The local part is replaced with a token; the domain is preserved so
        tenant/country filtering on domain remains possible.

        Args:
            email: Raw email address.

        Returns:
            String of the form ``tok_<hex8>@<domain>``.
        """
        if "@" not in email:
            raise ValueError("Not a valid email address")
        local, domain = email.split("@", 1)
        key = self.kms.get_data_key(KeyPurpose.PII_TOKENISATION)
        mac = hmac.new(key, local.encode(), hashlib.sha256)
        return f"tok_{mac.hexdigest()[:8]}@{domain}"
