import pytest
from datetime import date
from unittest.mock import MagicMock

from src.core.config import Settings, GarminSettings, LlmSettings, DataPathSettings
from src.agent.coach import Coach
from src.agent.actions import ActionSpec, NoDataError
from pydantic import BaseModel
import src.agent.actions as agent_actions

class MockParams(BaseModel):
    dummy: str = "test"

@pytest.fixture
def dummy_settings(tmp_path):
    return Settings(
        user_id="user1",
        garmin=GarminSettings(email="x", password="x"),
        data=DataPathSettings(root_dir=tmp_path),
        llm=LlmSettings(model="dummy", api_key="dummy")
    )

def test_coach_init(dummy_settings):
    coach = Coach(dummy_settings)
    assert coach.settings == dummy_settings

def test_coach_run_action_no_data(dummy_settings, monkeypatch):
    coach = Coach(dummy_settings, clock=lambda: date(2026, 1, 1))
    
    def raise_no_data(*args, **kwargs):
        raise NoDataError("No data found")
        
    mock_spec = ActionSpec(
        action_id="dummy_action",
        version=1,
        title="Dummy",
        params_model=MockParams,
        narrative_model=MockParams, # reuse
        prompt="hello",
        build_context=raise_no_data
    )
    
    monkeypatch.setitem(agent_actions.ACTIONS, "dummy_action", mock_spec)
    
    monkeypatch.setitem(agent_actions.ACTIONS, "dummy_action", mock_spec)
    
    result = coach.run_action_sync("dummy_action", {})
    assert result.status == "no_data"
    assert "No data found" in result.message

def test_coach_list_actions(dummy_settings):
    coach = Coach(dummy_settings)
    actions = coach.list_actions()
    assert isinstance(actions, list)
    assert len(actions) > 0
