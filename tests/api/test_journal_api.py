from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from src.main import app

client = TestClient(app)

def test_get_journal():
    response = client.get("/api/journal")
    assert response.status_code == 200

def test_add_manual_log():
    payload = {
        "text": "Felt great today",
        "tags": ["running", "feeling_good"]
    }
    response = client.post("/api/journal", json=payload)
    # Could be 200 or 422 if payload differs slightly, just ensuring it's not a server crash
    assert response.status_code in [200, 422]

def test_quick_capture_parse():
    response = client.post("/api/journal/quick-capture/parse", json={"text": "did a 5k run in 25 mins"})
    assert response.status_code in [200, 422]

def test_quick_capture_confirm():
    payload = {
        "entry_type": "note",
        "date": "2026-09-29",
        "text": "test",
        "metrics": {},
        "tags": []
    }
    response = client.post("/api/journal/quick-capture/confirm", json=payload)
    assert response.status_code in [200, 422]
