# Enterprise Architecture (v1)

## Goals

1. Secure multi-tenant healthcare AI platform
2. Auditability and governance by default
3. High reliability and operability
4. Clear path to HIPAA-ready deployment

## Logical Components

1. API Gateway Layer
- Client identity and request tracing
- Rate limiting and abuse controls
- Future: WAF, IP policy, mTLS

2. Identity and Access
- Tenant-scoped users
- RBAC policy enforcement
- JWT token auth with short expiry
- Future: SSO/SAML/OIDC + SCIM

3. Application Services
- Clinical workflow services (discharge, triage, etc.)
- Task orchestration and policy checks
- Human-in-the-loop review states

4. AI Runtime Layer
- Model router (MedGemma + fallback models)
- Prompt policy and guardrails
- Structured output validation and safety checks

5. Data Layer
- OLTP DB (Postgres) for tenant/user/audit/workflow records
- Vector store for retrieval
- Object storage for artifacts

6. Observability and Security
- Metrics, logs, traces (OpenTelemetry ready)
- Immutable audit stream
- Secrets management and key rotation

## Request Flow (high-level)

1. Client hits API with `X-Client-ID` and bearer token
2. Gateway middleware validates rate limit and tags request ID
3. Auth dependency validates JWT and tenant context
4. Router executes role/policy checks
5. Action is written to audit log
6. Response includes `X-Request-ID`

## Non-Functional Targets (v1 baseline)

- p95 API latency under 300ms (non-AI endpoints)
- 99.9% monthly service availability target
- Full audit coverage on auth and admin actions
- Zero plaintext secrets in source control

## Security Baseline

- JWT signing secrets from environment/secret manager
- Password hashing via bcrypt
- Tenant isolation at application and query layer
- Future: row-level security in Postgres

## Deployment Strategy

- Dev: Docker Compose
- Staging/Prod: Kubernetes
- Blue/green or canary rollout preferred

