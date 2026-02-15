# Sprint 1 Implementation Summary

## Completed Capabilities

1. Identity and Access
- JWT login endpoint
- `me` endpoint for token introspection
- Tenant-scoped user model
- RBAC dependencies for `admin`, `auditor`, `clinician`

2. Tenancy
- Tenant create/list endpoints
- Tenant slug uniqueness enforcement
- User creation scoped to actor tenant

3. Audit and Governance
- Audit log model and write helper
- Audit events for login, tenant creation, and user creation
- Audit read endpoint for `admin` and `auditor`

4. Gateway Controls
- Request ID middleware
- Required client ID header (`X-Client-ID`)
- In-memory rate limiting per client

5. Deployment Foundations
- Dockerfile
- Compose stack (API, Postgres, Redis)
- Kubernetes deployment/service manifests

## Sprint 2 Additions

1. Workflow Engine
- Workflow template create/list
- Workflow run create/list
- Job-backed asynchronous execution model

2. Policy Enforcement
- Policy rule model and API
- Conditional policy evaluation on:
  - `workflow.run.create`
  - `workflow.template.create`
  - `user.create`

3. Async Jobs
- Job list and one-shot dispatch endpoint
- Worker loop entrypoint (`enterprise_app.app.worker`)

4. Admin UI
- Browser console at `/admin` for:
  - login
  - policy creation
  - workflow template/run actions
  - job dispatch and monitoring

## API Surface

- `GET /health`
- `POST /v1/auth/login`
- `GET /v1/auth/me`
- `GET /v1/tenants`
- `POST /v1/tenants`
- `GET /v1/users`
- `POST /v1/users`
- `GET /v1/audit-logs`
- `GET /v1/policies`
- `POST /v1/policies`
- `POST /v1/policies/{policy_id}/toggle`
- `GET /v1/workflows/templates`
- `POST /v1/workflows/templates`
- `GET /v1/workflows/runs`
- `POST /v1/workflows/templates/{template_id}/runs`
- `GET /v1/jobs`
- `POST /v1/jobs/dispatch-once`
- `GET /admin`
