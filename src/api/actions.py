from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from src.agent.coach import Coach
from src.api.deps import get_coach

router = APIRouter(prefix="/api/actions", tags=["actions"])

@router.get("")
async def list_actions(coach: Annotated[Coach, Depends(get_coach)]):
    actions = coach.list_actions()
    return [{"action_id": a.action_id, "title": a.title, "version": a.version} for a in actions]

@router.post("/{action_id}/run")
async def run_action(
    action_id: str,
    params: dict[str, Any],
    coach: Annotated[Coach, Depends(get_coach)]
):
    try:
        result = await coach.run_action(action_id, params)
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="Action not found")
    except Exception as e:
        import pydantic
        if isinstance(e, pydantic.ValidationError):
            raise HTTPException(status_code=422, detail=e.errors())
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history")
async def action_history(
    coach: Annotated[Coach, Depends(get_coach)],
    limit: int = 50
):
    if not coach.insights:
        return []
    runs = coach.insights.list_runs(coach.settings.user_id, limit=limit)
    return runs
