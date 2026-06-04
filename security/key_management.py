"""
KMS-backed key management for the MAS platform.

Provides a unified interface over:
  - AWS KMS (for AWS-deployed agents: DataFetcher, Analyst, Gateway)
  - On-prem HashiCorp Vault (for on-prem agents: Orchestrator, Supervisor, Validator)

Keys are always fetched at runtime — never cached in memory beyond a single
request and never written to disk.  Key rotation is handled transparently by
the KMS provider.
"""
from __future__ import annotations

import base64
import os
from enum import Enum
from typing import Optional

from core.logging_config import get_logger

logger = get_logger(__name__)


class KeyPurpose(str, Enum):
    """Semantic key purpose labels — used to select the correct KMS key alias."""
    PII_TOKENISATION = "pii-tokenisation"
    REQUEST_SIGNING = "request-signing"
    DATA_ENCRYPTION = "data-encryption"
    JWT_SIGNING = "jwt-signing"


class KMSBackend(str, Enum):
    AWS = "aws"
    VAULT = "vault"


class KeyManagementClient:
    """
    Unified key management client with AWS KMS and HashiCorp Vault backends.

    Args:
        backend: Which KMS backend to use (auto-detected from environment
                 if not specified: ``KMS_BACKEND`` env var).

    Usage::

        kms = KeyManagementClient()
        key_bytes = kms.get_data_key(KeyPurpose.PII_TOKENISATION)
        encrypted = kms.encrypt(plaintext, KeyPurpose.DATA_ENCRYPTION)
        plaintext = kms.decrypt(ciphertext, KeyPurpose.DATA_ENCRYPTION)
    """

    def __init__(self, backend: Optional[KMSBackend] = None) -> None:
        self.backend = backend or KMSBackend(
            os.environ.get("KMS_BACKEND", KMSBackend.AWS.value)
        )

    def get_data_key(self, purpose: KeyPurpose) -> bytes:
        """
        Generate a new 256-bit data encryption key via the KMS.

        The KMS generates the key; the plaintext copy is returned for
        immediate use and the encrypted copy is discarded after use.
        The plaintext key is held in memory only for the duration of the
        calling function.

        Args:
            purpose: Semantic purpose that maps to a KMS key alias.

        Returns:
            32-byte (256-bit) plaintext data key.

        Raises:
            RuntimeError: If the KMS call fails.
        """
        if self.backend == KMSBackend.AWS:
            return self._aws_generate_data_key(purpose)
        return self._vault_generate_data_key(purpose)

    def encrypt(self, plaintext: bytes, purpose: KeyPurpose) -> bytes:
        """
        Encrypt plaintext bytes using the KMS master key for ``purpose``.

        Args:
            plaintext: Raw bytes to encrypt.
            purpose:   KMS key alias / purpose.

        Returns:
            Ciphertext bytes (KMS-specific format, base64 for AWS).
        """
        if self.backend == KMSBackend.AWS:
            return self._aws_encrypt(plaintext, purpose)
        return self._vault_encrypt(plaintext, purpose)

    def decrypt(self, ciphertext: bytes, purpose: KeyPurpose) -> bytes:
        """
        Decrypt ciphertext bytes using the KMS master key for ``purpose``.

        Args:
            ciphertext: Encrypted bytes (as returned by ``encrypt``).
            purpose:    KMS key alias / purpose.

        Returns:
            Decrypted plaintext bytes.
        """
        if self.backend == KMSBackend.AWS:
            return self._aws_decrypt(ciphertext, purpose)
        return self._vault_decrypt(ciphertext, purpose)

    # ── AWS KMS backend ──────────────────────────────────────────────────

    def _aws_key_id(self, purpose: KeyPurpose) -> str:
        """Return the AWS KMS key alias for a given purpose."""
        prefix = os.environ.get("AWS_KMS_KEY_PREFIX", "alias/mas-enterprise")
        return f"{prefix}-{purpose.value}"

    def _aws_generate_data_key(self, purpose: KeyPurpose) -> bytes:
        import boto3

        client = boto3.client(
            "kms",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        response = client.generate_data_key(
            KeyId=self._aws_key_id(purpose),
            KeySpec="AES_256",
        )
        # Return only the plaintext key; never log or store the encrypted copy
        return response["Plaintext"]

    def _aws_encrypt(self, plaintext: bytes, purpose: KeyPurpose) -> bytes:
        import boto3

        client = boto3.client(
            "kms",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        response = client.encrypt(
            KeyId=self._aws_key_id(purpose),
            Plaintext=plaintext,
        )
        return response["CiphertextBlob"]

    def _aws_decrypt(self, ciphertext: bytes, purpose: KeyPurpose) -> bytes:
        import boto3

        client = boto3.client(
            "kms",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        response = client.decrypt(
            CiphertextBlob=ciphertext,
            KeyId=self._aws_key_id(purpose),
        )
        return response["Plaintext"]

    # ── HashiCorp Vault backend (on-prem) ────────────────────────────────

    def _vault_transit_path(self, purpose: KeyPurpose) -> str:
        return f"transit/keys/mas-{purpose.value}"

    def _vault_generate_data_key(self, purpose: KeyPurpose) -> bytes:
        """Use Vault's Transit engine to generate a data key."""
        import hvac

        vault_addr = os.environ.get("VAULT_ADDR", "https://vault.internal:8200")
        vault_token = os.environ.get("VAULT_TOKEN", "")
        client = hvac.Client(url=vault_addr, token=vault_token)

        response = client.secrets.transit.generate_data_key(
            name=f"mas-{purpose.value}",
            key_type="plaintext",
        )
        # Vault returns base64-encoded plaintext
        return base64.b64decode(response["data"]["plaintext"])

    def _vault_encrypt(self, plaintext: bytes, purpose: KeyPurpose) -> bytes:
        import hvac

        vault_addr = os.environ.get("VAULT_ADDR", "https://vault.internal:8200")
        vault_token = os.environ.get("VAULT_TOKEN", "")
        client = hvac.Client(url=vault_addr, token=vault_token)

        b64_plaintext = base64.b64encode(plaintext).decode()
        response = client.secrets.transit.encrypt_data(
            name=f"mas-{purpose.value}",
            plaintext=b64_plaintext,
        )
        return response["data"]["ciphertext"].encode()

    def _vault_decrypt(self, ciphertext: bytes, purpose: KeyPurpose) -> bytes:
        import hvac

        vault_addr = os.environ.get("VAULT_ADDR", "https://vault.internal:8200")
        vault_token = os.environ.get("VAULT_TOKEN", "")
        client = hvac.Client(url=vault_addr, token=vault_token)

        response = client.secrets.transit.decrypt_data(
            name=f"mas-{purpose.value}",
            ciphertext=ciphertext.decode(),
        )
        return base64.b64decode(response["data"]["plaintext"])
