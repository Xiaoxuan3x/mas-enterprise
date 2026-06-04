"""
Zero-trust security utilities for the Multi-Agent System.

Implements:
  - JWT validation (RS256) against a configurable JWKS endpoint (Keycloak on-prem).
  - mTLS client-certificate fingerprint extraction.
  - RBAC role/scope enforcement.
  - Request signing for inter-agent API calls.

Keycloak is recommended as the on-prem replacement for Microsoft Entra ID.
It provides full OAuth 2.0, OIDC, and SAML 2.0 support with enterprise
features (federation, fine-grained authorisation, MFA) without a cloud
dependency.
"""
from __future__ import annotations

import hashlib
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt import PyJWKClient, PyJWKClientError

from core.logging_config import get_logger
from schemas.agent_io import SecurityContext

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_settings() -> Any:
    """Lazy import to avoid circular imports at module load time."""
    from config import Settings
    return Settings()  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────────────────────
# JWKS-backed JWT validation
# ─────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _jwks_client(jwks_uri: str) -> PyJWKClient:
    """
    Return a cached PyJWKClient for the given JWKS URI.

    Caching avoids a network round-trip on every request while the process
    is alive.  The cache is invalidated on process restart, ensuring fresh
    keys are fetched after rotation.

    Args:
        jwks_uri: Full URL of the JWKS endpoint (e.g., Keycloak realm JWKS).
    """
    return PyJWKClient(jwks_uri)


class JWTValidationError(Exception):
    """Raised when a JWT fails signature, expiry, or audience validation."""


def validate_jwt(
    token: str,
    jwks_uri: str,
    expected_audience: str,
    expected_issuer: str,
    required_roles: Optional[List[str]] = None,
) -> SecurityContext:
    """
    Validate a bearer JWT token against the on-prem Keycloak JWKS endpoint.

    Validates signature (RS256), expiry, issuer, audience, and optionally
    checks that the token carries all ``required_roles``.

    Args:
        token:            Raw JWT string (without the "Bearer " prefix).
        jwks_uri:         Keycloak JWKS URL, e.g.
                          ``https://keycloak.internal/realms/mas/protocol/openid-connect/certs``.
        expected_audience: The OAuth2 audience claim the token must contain.
        expected_issuer:   The issuer URL the token must match.
        required_roles:   Optional list of Keycloak realm roles that must all
                          be present in the token's ``realm_access.roles`` claim.

    Returns:
        A populated ``SecurityContext`` extracted from the validated claims.

    Raises:
        JWTValidationError: If signature, expiry, audience, issuer, or roles
                            check fails.

    Side effects:
        Logs ``security.jwt_validated`` on success and ``security.jwt_rejected``
        on failure.
    """
    try:
        client = _jwks_client(jwks_uri)
        signing_key = client.get_signing_key_from_jwt(token)
        payload: Dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience,
            issuer=expected_issuer,
        )
    except (PyJWKClientError, jwt.InvalidTokenError) as exc:
        logger.warning("security.jwt_rejected", reason=str(exc))
        raise JWTValidationError(f"JWT validation failed: {exc}") from exc

    realm_roles: List[str] = (
        payload.get("realm_access", {}).get("roles", [])
    )
    if required_roles:
        missing = set(required_roles) - set(realm_roles)
        if missing:
            logger.warning("security.rbac_denied", missing_roles=list(missing))
            raise JWTValidationError(
                f"Token is missing required roles: {missing}"
            )

    from datetime import datetime, timezone

    ctx = SecurityContext(
        subject=payload["sub"],
        tenant_id=payload.get("tenant_id", payload.get("azp", "unknown")),
        roles=realm_roles,
        scopes=payload.get("scope", "").split(),
        issuer=payload["iss"],
        issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        ip_address=payload.get("client_address"),
        device_id=payload.get("device_id"),
    )
    logger.info(
        "security.jwt_validated",
        subject=ctx.subject,
        tenant_id=ctx.tenant_id,
        roles=ctx.roles,
    )
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# mTLS client-certificate fingerprint
# ─────────────────────────────────────────────────────────────────────────────


def extract_mtls_fingerprint(pem_cert: bytes) -> str:
    """
    Compute the SHA-256 fingerprint of a PEM-encoded X.509 client certificate.

    Used to bind a request to the calling service's certificate identity for
    zero-trust service-to-service verification.

    Args:
        pem_cert: PEM-encoded certificate bytes (the value of the
                  ``X-Forwarded-Client-Cert`` or ``X-Client-Cert`` header
                  after URL-decoding).

    Returns:
        Colon-separated hex SHA-256 fingerprint string.
    """
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    cert = x509.load_pem_x509_certificate(pem_cert, default_backend())
    der = cert.public_bytes(
        encoding=__import__(
            "cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]
        ).Encoding.DER
    )
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


# ─────────────────────────────────────────────────────────────────────────────
# HMAC request signing for inter-agent API calls
# ─────────────────────────────────────────────────────────────────────────────


def sign_request(
    payload: bytes,
    secret_key: bytes,
    algorithm: str = "sha256",
) -> str:
    """
    Compute an HMAC signature for an inter-agent HTTP request body.

    Each agent includes this signature in the ``X-MAS-Signature`` header.
    The receiving agent verifies it before processing to prevent spoofed
    internal requests — part of the zero-trust model.

    Args:
        payload:   Raw request body bytes.
        secret_key: Shared secret retrieved from the KMS.
        algorithm: HMAC hash algorithm (default: "sha256").

    Returns:
        Hex-encoded HMAC digest string.
    """
    import hmac as _hmac

    mac = _hmac.new(secret_key, payload, algorithm)
    return mac.hexdigest()


def verify_request_signature(
    payload: bytes,
    received_signature: str,
    secret_key: bytes,
    algorithm: str = "sha256",
) -> bool:
    """
    Verify an HMAC request signature in constant time.

    Args:
        payload:            Raw request body bytes.
        received_signature: Hex string from the ``X-MAS-Signature`` header.
        secret_key:         Shared secret from KMS.
        algorithm:          Hash algorithm used during signing.

    Returns:
        True if the signature is valid; False otherwise.

    Side effects:
        Logs ``security.signature_valid`` or ``security.signature_invalid``.
    """
    import hmac as _hmac

    expected = sign_request(payload, secret_key, algorithm)
    valid = _hmac.compare_digest(expected, received_signature)
    if valid:
        logger.debug("security.signature_valid")
    else:
        logger.warning("security.signature_invalid")
    return valid
