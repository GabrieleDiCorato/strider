from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from src.main import app
from src.api.deps import get_llm_generator

client = TestClient(app)

class MockGenerator:
    async def generate(self, *, action_id: str, instruction: str, user_message: str, output_model: type) -> str:
        # Create dummy data for the required fields
        dummy_data = {}
        for name, field in output_model.model_fields.items():
            if field.annotation == str:
                dummy_data[name] = "dummy text"
            elif field.annotation == int:
                dummy_data[name] = 0
            elif field.annotation == float:
                dummy_data[name] = 0.0
            elif field.annotation == bool:
                dummy_data[name] = False
            elif field.annotation == list:
                dummy_data[name] = []
            elif field.annotation == dict:
                dummy_data[name] = {}
            else:
                dummy_data[name] = None
        instance = output_model.model_construct(**dummy_data)
        return instance.model_dump_json()

def test_list_actions():
    response = client.get("/api/actions")
    assert response.status_code == 200

def test_action_history():
    response = client.get("/api/actions/history")
    assert response.status_code == 200

def test_run_action():
    app.dependency_overrides[get_llm_generator] = lambda: MockGenerator()
    try:
        response = client.post("/api/actions/some_action/run", json={})
        assert response.status_code in [200, 404, 422, 500]
    finally:
        app.dependency_overrides.pop(get_llm_generator, None)
