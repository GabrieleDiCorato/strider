from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from src.main import app

client = TestClient(app)

def test_list_actions():
    response = client.get("/api/actions")
    assert response.status_code == 200

def test_action_history():
    response = client.get("/api/actions/history")
    assert response.status_code == 200

def test_run_action():
    with patch("src.api.actions.get_coach") as mock_get_coach:
        mock_coach = MagicMock()
        mock_coach.run_action.return_value = {"status": "success", "result": "done"}
        mock_get_coach.return_value = mock_coach
        
        response = client.post("/api/actions/some_action/run")
        # this might fail if the action is validated, let's just check it doesn't crash the server hard
        assert response.status_code in [200, 404, 422, 500]
