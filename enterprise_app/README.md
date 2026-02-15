# Enterprise AI Care Platform (Sprint 1)

This module is an enterprise-ready foundation for a healthcare AI platform.

## Delivered in Sprint 1

- Multi-tenant foundation (`Tenant`, `User`, `AuditLog`)
- JWT authentication with role-based access control (`admin`, `clinician`, `auditor`)
- API gateway middleware:
  - request ID propagation (`X-Request-ID`)
  - mandatory client identifier (`X-Client-ID`) on protected APIs
  - per-client rate limiting
- Audit logging for security-sensitive actions
- Deployment starter stack:
  - Dockerfile
  - Docker Compose (API + Postgres + Redis)
  - Kubernetes deployment/service manifests

## Delivered in Sprint 2

- Workflow engine foundation:
  - workflow templates
  - workflow runs
  - async execution jobs
- Async dispatch API and standalone worker loop
- Policy engine with conditional allow/deny rules
- Browser-based admin console at `/admin`

## Local Run

```bash
source /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/venv/bin/activate
uvicorn enterprise_app.app.main:app --host 0.0.0.0 --port 8020 --reload
```

## Default Bootstrap Admin

On first startup, the app seeds:

- tenant slug: `default`
- admin email: `admin@enterprise.local`
- admin password: `ChangeMe123!` (override via env)

## Quick API Example

```bash
curl -X POST "http://localhost:8020/v1/auth/login" \
  -H "Content-Type: application/json" \
  -H "X-Client-ID: local-dev" \
  -d '{"email":"admin@enterprise.local","password":"ChangeMe123!","tenant_slug":"default"}'
```

## Admin Console

Open:

- `http://localhost:8020/admin`

## Worker

Run async worker process:

```bash
source /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/venv/bin/activate
python -m enterprise_app.app.worker
```
