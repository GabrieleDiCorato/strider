import asyncio
from datetime import date, datetime, timezone
from typing import Any, Callable
import uuid

from pydantic import ValidationError

from src.core.config import Settings
from src.agent.actions import ACTIONS, ActionSpec, NoDataError
from src.agent.llm import NarrativeGenerator
from src.agent.tools import ToolContext
from src.agent.insights_log import InsightsLog, ActionResult, ActionStatus
from src.agent.prompts import BASE_SYSTEM_PROMPT, render_user_message
from src.core.schemas import EntrySource, JournalCategory

class Coach:
    def __init__(self, settings: Settings, *, generator: NarrativeGenerator | None = None,
                 tool_context: ToolContext | None = None, insights: InsightsLog | None = None,
                 clock: Callable[[], date] = lambda: datetime.now(timezone.utc).date()):
        self.settings = settings
        self.generator = generator
        self.tool_context = tool_context
        self.insights = insights
        self.clock = clock

    async def run_action(self, action_id: str, params: dict[str, Any]) -> ActionResult:
        spec = ACTIONS[action_id]
        parsed_params = spec.params_model.model_validate(params, context={"today": self.clock()})
        
        try:
            ctx = spec.build_context(self.tool_context, parsed_params, self.clock())
        except NoDataError as e:
            res = ActionResult(
                run_id=uuid.uuid4().hex,
                action_id=action_id,
                action_version=spec.version,
                status=ActionStatus.NO_DATA,
                message=str(e),
                created_at=datetime.now(timezone.utc)
            )
            if self.insights:
                self.insights.record(self.settings.user_id, action_id, spec.version, getattr(self.settings.llm, "model", "none"), parsed_params.model_dump_json(), "", res, str(e))
            return res

        context_json = ctx.payload.model_dump_json()
        params_json = parsed_params.model_dump_json()
        model_name = getattr(self.settings.llm, "model", "none")
        
        cache_key = self.insights._compute_cache_key(action_id, spec.version, model_name, params_json, context_json) if self.insights else ""
        
        if self.insights:
            cached = self.insights.find_cached(self.settings.user_id, cache_key)
            if cached:
                return ActionResult(
                    run_id=cached.run_id,
                    action_id=cached.action_id,
                    action_version=cached.action_version,
                    status=cached.status,
                    metrics=cached.metrics,
                    narrative=cached.narrative,
                    message=cached.message,
                    from_cache=True,
                    created_at=datetime.now(timezone.utc)
                )

        instruction = BASE_SYSTEM_PROMPT + "\\n\\n" + spec.prompt
        message = render_user_message(action_id, parsed_params, ctx.payload)
        
        narrative_obj = None
        error_str = None
        
        for _ in range(2):
            try:
                raw_text = await self.generator.generate(
                    action_id=action_id, instruction=instruction, user_message=message, output_model=spec.narrative_model
                )
                narrative_obj = spec.narrative_model.model_validate_json(raw_text)
                break
            except Exception as e:
                error_str = str(e)
                
        if not narrative_obj:
            res = ActionResult(
                run_id=uuid.uuid4().hex,
                action_id=action_id,
                action_version=spec.version,
                status=ActionStatus.FAILED,
                message="Couldn't generate this insight.",
                created_at=datetime.now(timezone.utc)
            )
            if self.insights:
                self.insights.record(self.settings.user_id, action_id, spec.version, model_name, params_json, context_json, res, error_str)
            return res

        if spec.writes_journal and self.tool_context and getattr(narrative_obj, "journal_observation", None):
            self.tool_context.journal.add_entry(
                user_id=self.settings.user_id,
                source=EntrySource.AI,
                category=JournalCategory.OBSERVATION,
                entry_date=ctx.journal_date,
                text=narrative_obj.journal_observation
            )
            
        res = ActionResult(
            run_id=uuid.uuid4().hex,
            action_id=action_id,
            action_version=spec.version,
            status=ActionStatus.SUCCESS,
            metrics=ctx.metrics,
            narrative=narrative_obj.model_dump(mode="json"),
            created_at=datetime.now(timezone.utc)
        )
        if self.insights:
            self.insights.record(self.settings.user_id, action_id, spec.version, model_name, params_json, context_json, res)
        return res

    def run_action_sync(self, action_id: str, params: dict[str, Any]) -> ActionResult:
        return asyncio.run(self.run_action(action_id, params))
        
    def list_actions(self) -> list[ActionSpec]:
        return list(ACTIONS.values())
