# MAS Enterprise — Multi-Agent System

Production-ready Multi-Agent System built with **LangGraph**, **Python 3.11**,
and a hybrid on-prem / multi-cloud architecture.

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
| Analyst | AWS | ECS + Bedrock | Optional Claude enrichment |
| Supervisor | On-Prem | NVIDIA B200/B300 | Gemini 2.5 Pro via NIM |
| Email Agent | Azure | ACS | azure-communication-email |
| Conversational | GCP | Dialogflow CX | Gemini fallback via Vertex AI |

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
