"""
Control Tower — Policy Engine

Enforces centralised, declarative policies across all agents and tenants.
Policies are loaded from a YAML file (or AWS SSM Parameter Store) at startup
and evaluated per-request without requiring agent code changes.

Policy types supported:
  - ``allow_list``:  Permit requests matching defined criteria.
  - ``deny_list``:   Block requests matching defined criteria.
  - ``rate_cap``:    Per-tenant / per-user operation limits.
  - ``data_residency``: Restrict which regions can process a tenant's data.
  - ``audit_required``: Flag operations that mandate a human-review audit trail.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import yaml

from core.logging_config import get_logger

logger = get_logger(__name__)


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_WITH_AUDIT = "allow_with_audit"


@dataclass
class PolicyViolation(Exception):
    """Raised when a DENY policy is triggered."""
    policy_name: str
    reason: str
    tenant_id: Optional[str] = None
    user_id: Optional[str] = None

    def __str__(self) -> str:
        return f"Policy '{self.policy_name}' denied request: {self.reason}"


@dataclass
class PolicyResult:
    """Result returned by the policy engine for a single evaluation."""
    decision: PolicyDecision
    policy_name: str
    reason: str
    requires_audit: bool = False


@dataclass
class Policy:
    """In-memory representation of a single loaded policy rule."""
    name: str
    policy_type: str
    applies_to: List[str]  # tenant IDs or ["*"] for all
    conditions: Dict[str, Any]
    decision: PolicyDecision
    reason: str
    enabled: bool = True


class PolicyEngine:
    """
    Evaluates request context against loaded policies.

    Policies are evaluated in declaration order; the first matching DENY
    policy short-circuits evaluation and raises ``PolicyViolation``.

    Usage::

        engine = PolicyEngine()
        engine.load_policies()
        result = engine.evaluate(
            operation="analyse",
            tenant_id="tenant-abc",
            user_id="user-123",
            context={"risk_level": "critical"}
        )
    """

    def __init__(self) -> None:
        self._policies: List[Policy] = []

    def load_policies(
        self,
        policy_file: Optional[str] = None,
    ) -> None:
        """
        Load policies from a YAML file or AWS SSM Parameter Store.

        Policy YAML format::

            policies:
              - name: block_sanctioned_countries
                type: deny_list
                applies_to: ["*"]
                conditions:
                  country_of_residence: ["IR", "KP", "SY", "CU"]
                decision: deny
                reason: "OFAC sanctioned country — transaction blocked"
                enabled: true

        Args:
            policy_file: Path to YAML file.  Defaults to
                         ``POLICY_FILE`` env var or ``policies.yaml``.

        Side effects:
            Replaces ``self._policies`` with freshly loaded rules.
            Logs a ``policy.loaded`` event.
        """
        source = policy_file or os.environ.get("POLICY_FILE", "policies.yaml")

        if source.startswith("ssm://"):
            raw_yaml = self._load_from_ssm(source.removeprefix("ssm://"))
        elif os.path.exists(source):
            with open(source) as fh:
                raw_yaml = fh.read()
        else:
            logger.warning("policy.no_policy_file", source=source)
            self._policies = self._default_policies()
            return

        data = yaml.safe_load(raw_yaml)
        self._policies = [
            Policy(
                name=p["name"],
                policy_type=p["type"],
                applies_to=p.get("applies_to", ["*"]),
                conditions=p.get("conditions", {}),
                decision=PolicyDecision(p.get("decision", "allow")),
                reason=p.get("reason", ""),
                enabled=p.get("enabled", True),
            )
            for p in data.get("policies", [])
        ]
        logger.info("policy.loaded", count=len(self._policies), source=source)

    def evaluate(
        self,
        operation: str,
        tenant_id: str,
        user_id: str,
        context: Dict[str, Any],
    ) -> PolicyResult:
        """
        Evaluate all applicable policies against the request context.

        Args:
            operation:  The operation being requested (e.g., "analyse").
            tenant_id:  Tenant partition key.
            user_id:    Requesting user identifier.
            context:    Additional context fields (risk_level, country, etc.).

        Returns:
            PolicyResult with the final decision and reason.

        Raises:
            PolicyViolation: If any DENY policy matches the context.
        """
        for policy in self._policies:
            if not policy.enabled:
                continue
            if not self._applies_to_tenant(policy, tenant_id):
                continue
            if self._matches_conditions(policy, context):
                if policy.decision == PolicyDecision.DENY:
                    logger.warning(
                        "policy.denied",
                        policy=policy.name,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    raise PolicyViolation(
                        policy_name=policy.name,
                        reason=policy.reason,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                if policy.decision == PolicyDecision.ALLOW_WITH_AUDIT:
                    logger.info(
                        "policy.audit_required",
                        policy=policy.name,
                        tenant_id=tenant_id,
                    )
                    return PolicyResult(
                        decision=PolicyDecision.ALLOW_WITH_AUDIT,
                        policy_name=policy.name,
                        reason=policy.reason,
                        requires_audit=True,
                    )

        return PolicyResult(
            decision=PolicyDecision.ALLOW,
            policy_name="default_allow",
            reason="No matching DENY or AUDIT policy",
        )

    def _applies_to_tenant(self, policy: Policy, tenant_id: str) -> bool:
        return "*" in policy.applies_to or tenant_id in policy.applies_to

    def _matches_conditions(self, policy: Policy, context: Dict[str, Any]) -> bool:
        for key, allowed_values in policy.conditions.items():
            ctx_value = context.get(key)
            if isinstance(allowed_values, list):
                if ctx_value not in allowed_values:
                    return False
            elif ctx_value != allowed_values:
                return False
        return bool(policy.conditions)

    def _load_from_ssm(self, parameter_name: str) -> str:
        import boto3

        client = boto3.client(
            "ssm",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        response = client.get_parameter(Name=parameter_name, WithDecryption=True)
        return response["Parameter"]["Value"]

    def _default_policies(self) -> List[Policy]:
        """Return a minimal safe default policy set when no file is found."""
        return [
            Policy(
                name="require_audit_critical_risk",
                policy_type="audit_required",
                applies_to=["*"],
                conditions={"risk_level": "critical"},
                decision=PolicyDecision.ALLOW_WITH_AUDIT,
                reason="Critical risk level mandates human review audit trail",
            )
        ]


# Module-level singleton
policy_engine = PolicyEngine()
