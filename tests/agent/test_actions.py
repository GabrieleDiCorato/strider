import pytest
from datetime import date
from unittest.mock import MagicMock

from src.agent.actions import (
    ReadinessParams, WeeklyDigestParams, ExplainActivityParams, ComparePeriodsParams,
    build_readiness_context, build_weekly_digest_context, build_explain_activity_context, build_compare_periods_context, NoDataError
)
from src.agent.tools import ToolContext

@pytest.fixture
def dummy_tool_context():
    tc = MagicMock(spec=ToolContext)
    tc.user_id = "test_user"
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = None
    cursor_mock.fetchall.return_value = []
    tc.gold = MagicMock()
    tc.gold.execute.return_value = cursor_mock
    tc.journal = MagicMock()
    tc.journal.list_entries.return_value = []
    tc.memory = MagicMock()
    tc.memory.read_memory.return_value = "Memory"
    return tc

def test_readiness_params_default(monkeypatch):
    monkeypatch.setattr("src.agent.actions.date", date)
    params = ReadinessParams.model_validate({}, context={"today": date(2026, 9, 29)})
    assert params.day is None

def test_build_readiness_no_data(dummy_tool_context):
    params = ReadinessParams(day=date(2026, 9, 29))
    with pytest.raises(NoDataError):
        build_readiness_context(dummy_tool_context, params, date(2026, 9, 29))

def test_build_weekly_no_data(dummy_tool_context):
    params = WeeklyDigestParams(week_start=date(2026, 9, 28))
    with pytest.raises(NoDataError):
        build_weekly_digest_context(dummy_tool_context, params, date(2026, 9, 29))

def test_build_explain_no_data(dummy_tool_context):
    params = ExplainActivityParams(activity_id="test_id")
    with pytest.raises(NoDataError):
        build_explain_activity_context(dummy_tool_context, params, date(2026, 9, 29))

def test_build_compare_no_data(dummy_tool_context):
    params = ComparePeriodsParams(
        period_a_start=date(2026, 9, 1),
        period_a_end=date(2026, 9, 7),
        period_b_start=date(2026, 9, 8),
        period_b_end=date(2026, 9, 14),
    )
    with pytest.raises(NoDataError):
        build_compare_periods_context(dummy_tool_context, params, date(2026, 9, 29))

