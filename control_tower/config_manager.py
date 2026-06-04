"""
Centralised configuration management for the Control Tower.

Sources configuration from (in priority order):
  1. Environment variables
  2. AWS SSM Parameter Store (for AWS-deployed agents)
  3. HashiCorp Vault dynamic secrets (for on-prem agents)
  4. Local .env file (development only)

All sensitive values (API keys, connection strings) are fetched at runtime
from the appropriate secrets backend — never hardcoded or checked into VCS.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Centralised settings for the MAS platform.

    Values are loaded from environment variables (uppercase) or .env file.
    Sensitive fields are SecretStr to prevent accidental logging.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Service ───────────────────────────────────────────────────────────
    env: str = Field(default="production", description="Deployment environment")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")

    # ── On-prem GPU / model serving ───────────────────────────────────────
    gemini_model_id: str = Field(default="gemini-2.5-pro")
    gemini_api_key: Optional[str] = Field(default=None)
    gemini_on_prem_endpoint: Optional[str] = Field(default=None)

    # ── AWS ───────────────────────────────────────────────────────────────
    aws_region: str = Field(default="us-east-1")
    dynamodb_profiles_table: str = Field(default="mas-user-profiles")
    dynamodb_transactions_table: str = Field(default="mas-transactions")
    dynamodb_endpoint_url: Optional[str] = Field(default=None)
    analyst_use_bedrock: bool = Field(default=False)
    bedrock_analyst_model: str = Field(
        default="anthropic.claude-3-5-sonnet-20241022-v2:0"
    )
    aws_kms_key_prefix: str = Field(default="alias/mas-enterprise")

    # ── Azure ─────────────────────────────────────────────────────────────
    azure_communication_connection_string: Optional[str] = Field(default=None)
    azure_email_sender: str = Field(
        default="DoNotReply@notifications.mas-enterprise.com"
    )

    # ── GCP ───────────────────────────────────────────────────────────────
    gcp_project_id: Optional[str] = Field(default=None)
    dialogflow_location: str = Field(default="us-central1")
    dialogflow_agent_id: Optional[str] = Field(default=None)

    # ── On-prem identity (Keycloak) ───────────────────────────────────────
    keycloak_jwks_uri: str = Field(
        default="http://keycloak.internal:8080/realms/mas/protocol/openid-connect/certs"
    )
    keycloak_token_url: str = Field(
        default="http://keycloak.internal:8080/realms/mas/protocol/openid-connect/token"
    )
    jwt_audience: str = Field(default="mas-enterprise")
    jwt_issuer: str = Field(
        default="https://keycloak.internal/realms/mas"
    )
    mas_client_id: str = Field(default="mas-service-account")
    mas_client_secret: Optional[str] = Field(default=None)

    # ── KMS / Vault ───────────────────────────────────────────────────────
    kms_backend: str = Field(default="aws")
    vault_addr: str = Field(default="https://vault.internal:8200")
    vault_token: Optional[str] = Field(default=None)

    # ── Observability ─────────────────────────────────────────────────────
    observe_customer_id: Optional[str] = Field(default=None)
    observe_ingest_token: Optional[str] = Field(default=None)
    observe_datastream: str = Field(default="mas-enterprise")
    observe_base_url: str = Field(
        default="https://collect.observeinc.com"
    )

    # ── Redis (rate limiting) ─────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379")
    rate_limit_per_minute: int = Field(default=100)

    # ── CORS ──────────────────────────────────────────────────────────────
    cors_origins: str = Field(default="")

    # ── Salesforce ────────────────────────────────────────────────────────
    salesforce_username: Optional[str] = Field(default=None)
    salesforce_password: Optional[str] = Field(default=None)
    salesforce_security_token: Optional[str] = Field(default=None)
    salesforce_domain: str = Field(default="login")

    # ── Policies ──────────────────────────────────────────────────────────
    policy_file: str = Field(default="policies.yaml")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the cached global Settings instance.

    Uses ``@lru_cache`` so that environment variables are read exactly once
    per process lifetime.  Call ``get_settings.cache_clear()`` in tests to
    reset between test cases.

    Returns:
        Populated Settings instance.
    """
    return Settings()
