from fastapi.testclient import TestClient
from codeweave.app import app

def test_health_reports_bootstrap_without_fake_capabilities():
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "bootstrap"
    assert body["package_version"] == "0.0.0"
    assert body["capabilities"]
    assert all(value is False for value in body["capabilities"].values())
