# Infrastructure Guide

This directory is the future home for production deployment assets for the
distributed MAS platform.

Current contents:

- `onprem/keycloak/realm-export.json`: local Keycloak realm for gateway auth and service-to-service identity flows
- `onprem/prometheus/prometheus.yml`: local Prometheus scrape configuration for the gateway and remote agent services

What this directory is for:

- infrastructure-as-code for AWS, Azure, GCP, and on-prem environments
- Kubernetes, ECS, Helm, or other deployment manifests
- identity, secrets, networking, and observability configuration
- environment-specific production overlays such as `dev`, `staging`, and `prod`

Application code stays outside this directory in `agents/`, `gateway/`,
`graph/`, `control_tower/`, `core/`, and `services/`.

## Current Gap

The codebase now supports distributed agent execution, but this directory does
not yet provision live cloud infrastructure. That means the repo can run the
distributed topology locally, but it does not yet stand up:

- AWS networking, compute, and managed data services
- Azure deployment for the email agent
- GCP deployment for the conversational agent
- on-prem production clusters for the orchestrator, validator, and supervisor
- production-grade secret distribution, private networking, and deployment automation

## Recommended Next Steps

### 1. Choose the primary IaC tool

Use one consistent tool for infrastructure provisioning.

Recommended default:

- `Terraform` for cloud and shared platform resources

Possible split:

- `Terraform` for AWS, Azure, GCP, DNS, IAM, secrets integration
- `Helm` or `Kustomize` for Kubernetes deployment manifests

### 2. Define the target production topology

Before writing IaC, lock the deployment model for each service.

Suggested baseline:

- `Gateway`: AWS ECS or EKS
- `DataFetcher`: AWS ECS, EKS, or Lambda
- `Analyst`: AWS ECS or EKS
- `DataValidator`: on-prem Kubernetes or VM service
- `Supervisor`: on-prem GPU-backed Kubernetes or VM service
- `Email Agent`: Azure container app, AKS, or simple service host
- `Conversational Agent`: GCP Cloud Run, GKE, or managed service integration

### 3. Create a real folder structure

Suggested shape:

```text
infrastructure/
  terraform/
    modules/
      aws-network/
      aws-compute/
      aws-data/
      gcp-conversational/
      azure-email/
      onprem-platform/
      observability/
      identity/
    environments/
      dev/
      staging/
      prod/
  kubernetes/
    base/
    overlays/
      dev/
      staging/
      prod/
  onprem/
    keycloak/
    prometheus/
    vault/
  scripts/
```

### 4. Start with the minimum production-critical resources

Provision these first:

- VPC/VNet/subnets and routing
- security groups/firewalls
- compute runtime for each service
- container registry and image promotion path
- DynamoDB or equivalent data stores
- Redis
- Keycloak and Vault production deployment
- secrets wiring for `INTER_SERVICE_HMAC_KEY`, cloud credentials, and model/API keys

### 5. Add service-to-service production controls

The code already supports signed inter-service requests. Production infra
should add:

- mTLS or service mesh where appropriate
- internal DNS/service discovery
- private ingress between orchestrator and remote agents
- least-privilege IAM or workload identity per service
- key rotation for signing and encryption keys

### 6. Add deployment automation

You will need:

- CI build pipeline for container images
- image tagging and promotion rules
- environment deployment pipeline
- rollback strategy
- configuration separation for `dev`, `staging`, and `prod`

### 7. Add production observability

Implement:

- central log aggregation
- Observe integration per service
- Prometheus scraping and alerting
- dashboards for request volume, latency, retries, failures, and token usage
- distributed tracing or correlation via `request_id`

### 8. Add environment validation gates

Before production cutover, add:

- infrastructure plan/apply validation
- smoke tests after deployment
- connectivity tests across clouds and on-prem
- secrets and identity verification
- staged rollout or canary strategy

## Practical Order Of Work

If you want the lowest-risk path, do the infrastructure work in this order:

1. Deploy on-prem components: Keycloak, Vault, orchestrator, validator, supervisor.
2. Deploy AWS components: gateway, data fetcher, analyst, DynamoDB, Redis.
3. Deploy Azure email agent.
4. Deploy GCP conversational agent and Dialogflow integration.
5. Add full observability, private connectivity, and deployment automation.

## Short Version

Treat `infrastructure/` as the place that turns this repo from
"distributed application code" into "real production platform."
