from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from src.main import app
from src.core.schemas import EntityType

client = TestClient(app)


def test_sync_garmin_api_json():
    mock_counts = {
        EntityType.ACTIVITY: 5,
        EntityType.DAILY_SUMMARY: 3,
        EntityType.WORKOUT_DEFINITION: 1,
        EntityType.WORKOUT_CALENDAR: 2,
    }
    with patch("src.api.sync.run_pipeline", return_value=mock_counts):
        response = client.post("/api/sync")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["counts"]["activity"] == 5


def test_sync_garmin_api_htmx_success():
    mock_counts = {
        EntityType.ACTIVITY: 5,
        EntityType.DAILY_SUMMARY: 3,
        EntityType.WORKOUT_DEFINITION: 1,
        EntityType.WORKOUT_CALENDAR: 2,
    }
    with patch("src.api.sync.run_pipeline", return_value=mock_counts):
        response = client.post("/api/sync", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert "Synced (11 records)" in response.text
        assert "Sync Garmin" in response.text


def test_sync_garmin_api_htmx_error():
    with patch("src.api.sync.run_pipeline", side_effect=RuntimeError("Garmin connection timeout")):
        response = client.post("/api/sync", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert "Sync Failed" in response.text
        assert "Garmin connection timeout" in response.text
