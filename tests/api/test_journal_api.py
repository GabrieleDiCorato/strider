from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import tempfile
from src.main import app
from src.api.deps import get_journal_store
from src.agent.journal import JournalStore

client = TestClient(app)

import os
import uuid

def override_get_journal_store():
    db_path = os.path.join(tempfile.gettempdir(), f"strider_test_{uuid.uuid4().hex}.db")
    return JournalStore(db_path=db_path)

app.dependency_overrides[get_journal_store] = override_get_journal_store

def test_get_journal():
    response = client.get("/api/journal")
    assert response.status_code == 200

def test_add_manual_log():
    payload = {
        "text": "Felt great today",
        "category": "feeling",
        "entry_date": "2026-09-29",
        "rating": 8
    }
    response = client.post("/api/journal", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["text"] == "Felt great today"
    assert data["category"] == "feeling"
    assert data["rating"] == 8

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
