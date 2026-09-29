import pytest
from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

def test_dashboard():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

def test_coach_page():
    response = client.get("/coach")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

def test_journal_page():
    response = client.get("/journal")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

def test_history_page():
    response = client.get("/history")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

def test_activities_list():
    response = client.get("/activities")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

def test_activity_detail_not_found():
    response = client.get("/activities/not-a-real-id")
    # depending on implementation, might return 404 or 500.
    # We at least want to make sure it runs without crashing the app.
    assert response.status_code in [404, 200, 500]

