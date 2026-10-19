from fastapi.testclient import TestClient
from codeweave.app import app

def test_health_reports_query_capabilities_without_claiming_an_index():
    response = TestClient(app, base_url="http://localhost").get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "m1"
    assert body["database_configured"] is False
    assert body["capabilities"]["structural_queries"] is True
    assert body["capabilities"]["workspace_updates"] is False
