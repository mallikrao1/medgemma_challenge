from fastapi.testclient import TestClient

from enterprise_app.app.main import app


def _login(client: TestClient, client_id: str = "test-suite") -> tuple[dict, str]:
    headers = {"X-Client-ID": client_id}
    login = client.post(
        "/v1/auth/login",
        headers=headers,
        json={"email": "admin@enterprise.local", "password": "ChangeMe123!", "tenant_slug": "default"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {**headers, "Authorization": f"Bearer {token}"}, token


def test_workflow_job_dispatch_end_to_end():
    with TestClient(app) as client:
        headers, _ = _login(client, client_id="s2-dispatch")

        templates = client.get("/v1/workflows/templates", headers=headers)
        assert templates.status_code == 200, templates.text
        assert len(templates.json()) >= 1
        template_id = templates.json()[0]["id"]

        run_resp = client.post(
            f"/v1/workflows/templates/{template_id}/runs",
            headers=headers,
            json={"input_data": {"risk_score": 3, "case_id": "abc"}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]

        dispatch = client.post("/v1/jobs/dispatch-once", headers=headers)
        assert dispatch.status_code == 200, dispatch.text
        if dispatch.json() is not None:
            assert dispatch.json()["status"] in {"completed", "failed", "retry"}

        runs = client.get("/v1/workflows/runs", headers=headers)
        assert runs.status_code == 200, runs.text
        found = [row for row in runs.json() if row["id"] == run_id]
        assert found, "Workflow run not found after dispatch"
        assert found[0]["status"] in {"queued", "running", "completed", "failed"}


def test_policy_denies_workflow_run_when_condition_matches():
    with TestClient(app) as client:
        headers, _ = _login(client, client_id="s2-policy")

        created_policy = client.post(
            "/v1/policies",
            headers=headers,
            json={
                "name": "Block very high risk",
                "target_action": "workflow.run.create",
                "effect": "deny",
                "condition": {"field": "input.risk_score", "op": "gt", "value": 8},
            },
        )
        assert created_policy.status_code == 201, created_policy.text

        templates = client.get("/v1/workflows/templates", headers=headers)
        assert templates.status_code == 200, templates.text
        template_id = templates.json()[0]["id"]

        denied = client.post(
            f"/v1/workflows/templates/{template_id}/runs",
            headers=headers,
            json={"input_data": {"risk_score": 9}},
        )
        assert denied.status_code == 403, denied.text

