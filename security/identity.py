"""
On-premises identity management integration with Keycloak.

Keycloak is the recommended replacement for Microsoft Entra ID (Azure AD)
for on-prem deployments.  It provides:

  - Full OAuth 2.0 / OIDC / SAML 2.0 protocol support
  - Realm-level multi-tenancy (one realm per business unit or tenant)
  - Fine-grained authorisation services (UMA 2.0)
  - Hardware-backed MFA (TOTP, WebAuthn/FIDO2)
  - User federation (LDAP/Active Directory sync)
  - Brokering to external IdPs
  - Zero cloud dependency — runs on Kubernetes or bare metal

Migration path from Microsoft Entra ID:
  1. Deploy Keycloak on-prem (HA cluster: 3 nodes minimum).
  2. Enable LDAP user federation to sync existing AD users.
  3. Configure SAML 2.0 SP for legacy apps that used ADFS.
  4. Migrate modern apps to OIDC (client credentials for M2M, auth-code for humans).
  5. Decommission Entra ID external dependency after validation period.

This module provides helper functions for the service account (M2M) flow
used by the MAS agents to obtain access tokens for inter-service calls.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from core.logging_config import get_logger

logger = get_logger(__name__)

_token_cache: dict = {}


def get_service_account_token(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    token_url: Optional[str] = None,
    scope: str = "mas:internal",
) -> str:
    """
    Obtain a service-account (client credentials flow) access token from
    the on-prem Keycloak realm.

    Tokens are cached in memory until 60 seconds before expiry to avoid
    unnecessary token requests on every inter-agent call.

    Args:
        client_id:     OAuth 2.0 client ID (defaults to ``MAS_CLIENT_ID`` env).
        client_secret: Client secret (defaults to ``MAS_CLIENT_SECRET`` env).
        token_url:     Full token endpoint URL (defaults to
                       ``KEYCLOAK_TOKEN_URL`` env).
        scope:         Requested OAuth scopes.

    Returns:
        Raw JWT access token string.

    Raises:
        RuntimeError: If the token request fails or the response is invalid.

    Side effects:
        Logs ``identity.token_obtained`` or ``identity.token_refreshed`` events.
        Caches the token until near-expiry.
    """
    _client_id = client_id or os.environ.get("MAS_CLIENT_ID", "mas-service-account")
    _client_secret = client_secret or os.environ.get("MAS_CLIENT_SECRET", "")
    _token_url = token_url or os.environ.get(
        "KEYCLOAK_TOKEN_URL",
        "http://keycloak.internal:8080/realms/mas/protocol/openid-connect/token",
    )

    cache_key = f"{_client_id}:{_token_url}"
    cached = _token_cache.get(cache_key)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["access_token"]

    response = httpx.post(
        _token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": _client_id,
            "client_secret": _client_secret,
            "scope": scope,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10.0,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Keycloak token request failed: {response.status_code} {response.text[:200]}"
        )

    data = response.json()
    access_token: str = data["access_token"]
    expires_in: int = data.get("expires_in", 300)

    _token_cache[cache_key] = {
        "access_token": access_token,
        "expires_at": time.time() + expires_in,
    }

    action = "identity.token_refreshed" if cached else "identity.token_obtained"
    logger.info(action, client_id=_client_id, expires_in=expires_in)

    return access_token


def introspect_token(
    token: str,
    introspection_url: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> dict:
    """
    Introspect a token via the Keycloak token introspection endpoint.

    Used by agents to validate tokens received from sibling agents without
    maintaining a local JWKS cache.

    Args:
        token:             JWT access token to introspect.
        introspection_url: Keycloak introspection endpoint URL.
        client_id:         Resource server client ID.
        client_secret:     Resource server client secret.

    Returns:
        Introspection response dict (``active``, ``sub``, ``scope``, etc.).

    Raises:
        RuntimeError: On network or authentication errors.
    """
    _url = introspection_url or os.environ.get(
        "KEYCLOAK_INTROSPECT_URL",
        "http://keycloak.internal:8080/realms/mas/protocol/openid-connect/token/introspect",
    )
    _client_id = client_id or os.environ.get("MAS_CLIENT_ID", "mas-service-account")
    _client_secret = client_secret or os.environ.get("MAS_CLIENT_SECRET", "")

    response = httpx.post(
        _url,
        data={"token": token, "client_id": _client_id, "client_secret": _client_secret},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=5.0,
    )

    if response.status_code != 200:
        raise RuntimeError(f"Token introspection failed: {response.status_code}")

    return response.json()
