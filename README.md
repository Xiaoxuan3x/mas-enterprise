# MAS Enterprise — Multi-Agent System

Production-ready Multi-Agent System built with **LangGraph**, **Python 3.11**,
and a hybrid on-prem / multi-cloud architecture.

## High-Level Architecture

This repository implements a hybrid **Python + LangGraph** Multi-Agent System
with an **on-prem Orchestrator**, multiple **deterministic sub-agents**, and one
primary **non-deterministic LLM-backed supervisor**.

```text
Client Request
  -> Gateway / Ingress
  -> On-Prem Orchestrator
  -> Deterministic Agents
     - DataFetcher
     - DataValidator
     - Analyst
  -> Non-Deterministic Agent
     - Supervisor (Gemini-backed)
  -> Optional Sidecar Agents
     - Email Agent
     - Conversational Agent
  -> Final Response
```

Current platform split in this repo:

- On-prem: Orchestrator, policy gates, validation, supervisor, identity/security controls
- AWS: Gateway hosting assumptions, DynamoDB-backed fetching, analyst enrichment path
- GCP / Google: Gemini model usage and Dialogflow conversational path
- Azure: Notification email delivery

This means the repository **does achieve** the core MAS shape:

- Python implementation
- LangGraph-based coordination
- deterministic and non-deterministic agent mix
- central on-prem Orchestrator
- hybrid deployment model

The repo is therefore best described as a **hybrid enterprise MAS with
Google-led AI services**, not a fully Google-centric infrastructure stack.

## Requirement Coverage

### Architecture and Roles

- `A.1 Central Orchestrator`: [agents/orchestrator.py](/Users/trix/dev/mas-enterprise/agents/orchestrator.py) manages state, routing, finalization, and failure handling.
- `A.2 Non-Deterministic Agent C (Supervisor)`: [agents/supervisor.py](/Users/trix/dev/mas-enterprise/agents/supervisor.py) is Gemini-backed, handles graceful degradation, and aggregates natural-language output.
- `A.3 Deterministic Agent B (DataValidator)`: [agents/data_validator.py](/Users/trix/dev/mas-enterprise/agents/data_validator.py) uses schema checks, regex, and math/business rules.
- `A.4 Deterministic Agent A (DataFetcher)`: [agents/data_fetcher.py](/Users/trix/dev/mas-enterprise/agents/data_fetcher.py) performs code-only structured data retrieval.
- `A.5 Deterministic Agent A (Analyst)`: [agents/analyst.py](/Users/trix/dev/mas-enterprise/agents/analyst.py) performs risk scoring, fraud analysis, backtesting, and optional LLM-assisted explanation enrichment.
- `A.6 Email Agent`: [agents/email_agent.py](/Users/trix/dev/mas-enterprise/agents/email_agent.py) sends notifications through Azure Communication Services.
- `A.7 Conversational Agent`: [agents/conversational_agent.py](/Users/trix/dev/mas-enterprise/agents/conversational_agent.py) runs on the GCP/Dialogflow path and integrates with Salesforce.
- `A.8 Other Considerations`: gateway defense, tokenisation utilities, key management, policy control tower, and zero-trust identity are implemented across [gateway/](/Users/trix/dev/mas-enterprise/gateway), [security/](/Users/trix/dev/mas-enterprise/security), and [control_tower/](/Users/trix/dev/mas-enterprise/control_tower).

### Enterprise Controls

- `B.1 State Management`: centralized `MASState` with append-only execution/error reducers in [schemas/state.py](/Users/trix/dev/mas-enterprise/schemas/state.py).
- `B.2 Type Safety & Validation`: strict Pydantic contracts in [schemas/agent_io.py](/Users/trix/dev/mas-enterprise/schemas/agent_io.py).
- `B.3 Error Handling & Fallbacks`: deterministic retry helpers in [core/retry.py](/Users/trix/dev/mas-enterprise/core/retry.py), deterministic retries in fetch/email/analyst paths, and supervisor graceful fallback on LLM failure.
- `B.4 Guardrails`: supervisor output guardrails in [core/guardrails.py](/Users/trix/dev/mas-enterprise/core/guardrails.py).
- `B.5 Logging & Observability`: structured JSON logs, Observe emission, and Prometheus metrics in [core/logging_config.py](/Users/trix/dev/mas-enterprise/core/logging_config.py) and [observability/](/Users/trix/dev/mas-enterprise/observability).
- `B.6 Control Tower`: per-request policy evaluation and centralized config in [control_tower/](/Users/trix/dev/mas-enterprise/control_tower).
- `B.7 Security`: JWT validation, optional mTLS enforcement, HMAC signing, tokenisation helpers, and KMS/Vault-backed key management in [core/security.py](/Users/trix/dev/mas-enterprise/core/security.py) and [security/](/Users/trix/dev/mas-enterprise/security).

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Docker + Docker Compose
- Access credentials for AWS, Azure, and GCP (or mock endpoints for local dev)

### 2. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

### 4. Start local infrastructure

```bash
docker-compose up -d dynamodb-local redis keycloak vault
```

### 5. Run the gateway

```bash
python main.py
# or
uvicorn gateway.ingress:app --reload --port 8000
```

### 6. Send a test request

```bash
# First, get a token from Keycloak (local dev)
TOKEN=$(curl -s -X POST http://localhost:8080/realms/mas/protocol/openid-connect/token \
  -d "grant_type=client_credentials" \
  -d "client_id=mas-service-account" \
  -d "client_secret=your-secret" \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -X POST http://localhost:8000/analyse \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: $(python -c 'import uuid; print(uuid.uuid4())')" \
  -d '{
    "user_id": "user_test_001",
    "tenant_id": "tenant_acme",
    "fetch_types": ["profile", "transactions"],
    "date_range_days": 90,
    "send_email": true,
    "notification_email": "compliance@acme.com"
  }'
```

---

## Running Tests

```bash
pytest tests/ -v
# Run with coverage
pytest tests/ -v --cov=. --cov-report=term-missing
```

These tests are mostly mocked:

- no real DynamoDB, Gemini, or Azure calls are required
- the suite validates your pipeline logic and guardrail behavior, not live cloud services

---

## Project Structure

```
mas-enterprise/
├── main.py                         # Application entry point
├── schemas/
│   ├── state.py                    # LangGraph MASState TypedDict + initial_state()
│   └── agent_io.py                 # All Pydantic I/O models for every agent boundary
├── agents/
│   ├── orchestrator.py             # Central coordinator, routing functions, finalize
│   ├── data_fetcher.py             # AWS DynamoDB data retrieval (Deterministic A)
│   ├── data_validator.py           # Rule-based validation engine (Deterministic B)
│   ├── analyst.py                  # Fraud scoring + backtesting (Deterministic A)
│   ├── policy_gate.py              # Control Tower policy checkpoints in the graph
│   ├── supervisor.py               # Gemini LLM summary + recs (Non-Deterministic C)
│   ├── email_agent.py              # Azure Communication Services email
│   └── conversational_agent.py     # GCP Dialogflow CX + Gemini fallback
├── core/
│   ├── logging_config.py           # structlog JSON structured logging
│   ├── retry.py                    # Tenacity exponential backoff decorators
│   ├── guardrails.py               # LLM output validation and forbidden token scan
│   └── security.py                 # JWT/mTLS/HMAC zero-trust utilities
├── graph/
│   └── workflow.py                 # LangGraph StateGraph definition + build_workflow()
├── gateway/
│   ├── ingress.py                  # FastAPI gateway (JWT, rate limit, injection guard)
│   ├── prompt_injection_guard.py   # Regex-based injection pattern scanner
│   └── pii_obfuscator.py           # PII scrubbing and user ID tokenisation
├── security/
│   ├── key_management.py           # AWS KMS + HashiCorp Vault unified KMS client
│   ├── tokenization.py             # Format-preserving PAN/email/user-ID tokenisation
│   └── identity.py                 # Keycloak service account token management
├── observability/
│   ├── observe_client.py           # Observe HTTP ingest event shipping
│   └── metrics.py                  # Prometheus counters and histograms
├── control_tower/
│   ├── policy_engine.py            # YAML-driven policy evaluation (allow/deny/audit)
│   └── config_manager.py           # Pydantic Settings — centralised configuration
├── integrations/
│   └── salesforce_client.py        # Salesforce CRM case + activity logging
├── tests/
│   ├── conftest.py                 # Shared fixtures (state, models, mock factories)
│   ├── test_e2e_success.py         # End-to-end happy path + invalid-data path
│   ├── test_supervisor_failure.py  # Supervisor failure modes + guardrail unit tests
│   └── test_data_validator.py      # DataValidator rule engine unit tests
├── policies.yaml                   # Declarative policy rules (deny/audit/allow)
├── requirements.txt
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── .env.example
└── ARCHITECTURE.md                 # Data flow diagrams and platform mapping
```

---

## Agent Platform Mapping

| Agent | Platform | Compute | Notes |
|---|---|---|---|
| Orchestrator | On-Prem | CPU | LangGraph routing node |
| DataFetcher | AWS | ECS/Lambda | DynamoDB via boto3 |
| DataValidator | On-Prem | CPU | Rule engine — no LLM |
| Analyst | AWS | ECS + optional Bedrock | Deterministic scoring with optional LLM narrative enrichment |
| Supervisor | On-Prem | NVIDIA B200/B300 | Gemini 2.5 Pro via NIM |
| Email Agent | Azure | ACS | azure-communication-email |
| Conversational | GCP | Dialogflow CX | Gemini fallback via Vertex AI |

Google is the primary AI provider in this design through the Supervisor and Conversational flows, while AWS and Azure provide data and communication integrations required by the requested hybrid enterprise topology.

---

## On-Prem Identity: Keycloak vs Microsoft Entra ID

This system uses **Keycloak** as the on-premises identity provider, replacing
Microsoft Entra ID (Azure AD). Keycloak provides full OAuth 2.0/OIDC/SAML
support without a cloud dependency. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the migration path.

**Configuration:**
```bash
KEYCLOAK_JWKS_URI=https://keycloak.your-domain.internal/realms/mas/protocol/openid-connect/certs
JWT_AUDIENCE=mas-enterprise
JWT_ISSUER=https://keycloak.your-domain.internal/realms/mas
```

---

## Enterprise Controls

| Control | Implementation |
|---|---|
| **B.1 State Management** | LangGraph MASState TypedDict with `operator.add` reducers |
| **B.2 Type Safety** | Pydantic v2 on every agent I/O boundary — raises at parse time |
| **B.3 Retry + Backoff** | Tenacity 3-attempt exponential jitter on all deterministic agents |
| **B.3 LLM Fallback** | `build_fallback_supervisor_output()` on API error/guardrail failure |
| **B.4 Guardrails** | Schema + structural + forbidden-token scan on SupervisorOutput |
| **B.5 Observability** | structlog JSON → Observe HTTP ingest + Prometheus metrics |
| **B.6 Control Tower** | `PolicyEngine` evaluates YAML policies per request |
| **B.7 Zero Trust** | JWT (Keycloak), mTLS fingerprint, HMAC inter-agent signing |

---

## Configuration Reference

All settings are in `control_tower/config_manager.py` as a Pydantic `Settings`
class. Key settings:

| Variable | Default | Description |
|---|---|---|
| `GEMINI_MODEL_ID` | `gemini-2.5-pro` | Supervisor model |
| `GEMINI_ON_PREM_ENDPOINT` | `""` | Set to route to on-prem NIM |
| `ANALYST_USE_BEDROCK` | `false` | Enable Bedrock narrative enrichment |
| `KMS_BACKEND` | `aws` | `aws` or `vault` |
| `POLICY_FILE` | `policies.yaml` | Or `ssm:///path/to/param` |
| `OBSERVE_CUSTOMER_ID` | `""` | Observe customer ID for telemetry |
| `RATE_LIMIT_PER_MINUTE` | `100` | Per-tenant request cap |

---

## GPU Deployment (NVIDIA B200/B300)

For on-prem Gemini serving on NVIDIA hardware, use **NVIDIA AI Enterprise**
with the NIM (NVIDIA Inference Microservices) container:

```bash
# Pull and run the Gemini-compatible NIM endpoint
docker run --gpus all \
  -e NGC_API_KEY=$NGC_API_KEY \
  -p 8000:8000 \
  nvcr.io/nim/google/gemini-2.5-pro:latest

# Configure MAS to route to this endpoint
export GEMINI_ON_PREM_ENDPOINT=http://nim-host:8000/v1
```

Alternatively, use **vLLM** with an open-weight model (e.g., Llama-3-70B)
as a Gemini-API-compatible drop-in when on-prem Gemini licensing is not available.
