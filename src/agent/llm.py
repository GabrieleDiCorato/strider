import os
from typing import Protocol
from pydantic import BaseModel

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from src.core.config import LlmSettings


class LLMGenerator(Protocol):
    async def generate(self, *, action_id: str, instruction: str, user_message: str,
                       output_model: type[BaseModel]) -> str: ...


class AdkLLMGenerator:
    def __init__(self, settings: LlmSettings):
        self._settings = settings
        if settings.api_key:
            os.environ.setdefault("GOOGLE_API_KEY", settings.api_key.get_secret_value())

    async def generate(self, *, action_id: str, instruction: str, user_message: str, output_model: type[BaseModel]) -> str:
        agent = LlmAgent(
            name=action_id,
            model=self._settings.model,
            instruction=lambda _ctx: instruction,
            output_schema=output_model,
            include_contents="none",
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
            generate_content_config=types.GenerateContentConfig(
                temperature=self._settings.temperature,
                max_output_tokens=self._settings.max_output_tokens
            )
        )
        runner = InMemoryRunner(agent=agent, app_name="strider")
        session = await runner.session_service.create_session(app_name="strider", user_id="strider-action")
        message = types.Content(role="user", parts=[types.Part(text=user_message)])
        final = ""
        async for event in runner.run_async(user_id="strider-action", session_id=session.id, new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(part.text or "" for part in event.content.parts)
        if not final:
            raise RuntimeError("Model returned no final response.")
        return final

