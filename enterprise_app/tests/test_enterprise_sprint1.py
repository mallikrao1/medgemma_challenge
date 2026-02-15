import uuid

from fastapi.testclient import TestClient

from enterprise_app.app.main import app


def test_gateway_requires_client_id_header():
    with TestClient(app) as client:
        response = client.post(
            "/v1/auth/login",
            json={
                "email": "admin@enterprise.local",
                "password": "ChangeMe123!",
                "tenant_slug": "default",
            },
        )
        assert response.status_code == 400
        assert "Missing required header" in response.json()["detail"]


def test_auth_tenant_user_audit_flow():
    with TestClient(app) as client:
        headers = {"X-Client-ID": "test-suite"}

        login = client.post(
            "/v1/auth/login",
            headers=headers,
            json={
                "email": "admin@enterprise.local",
                "password": "ChangeMe123!",
                "tenant_slug": "default",
            },
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        auth_headers = {**headers, "Authorization": f"Bearer {token}"}

        tenant_slug = f"t-{uuid.uuid4().hex[:8]}"
        created_tenant = client.post(
            "/v1/tenants",
            headers=auth_headers,
            json={"name": "Acme Care", "slug": tenant_slug},
        )
        assert created_tenant.status_code == 201, created_tenant.text

        created_user = client.post(
            "/v1/users",
            headers=auth_headers,
            json={
                "email": f"clinician-{uuid.uuid4().hex[:6]}@acme.local",
                "full_name": "Jane Clinician",
                "role": "clinician",
                "password": "StrongPass123!",
            },
        )
        assert created_user.status_code == 201, created_user.text

        audits = client.get("/v1/audit-logs?limit=20", headers=auth_headers)
        assert audits.status_code == 200, audits.text
        actions = [row["action"] for row in audits.json()]
        assert "tenant.create" in actions
        assert "user.create" in actions

