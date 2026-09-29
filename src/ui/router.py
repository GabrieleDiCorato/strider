from typing import Annotated, Optional
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.core.config import Settings, get_settings
from src.agent.coach import Coach
from src.agent.journal import JournalStore
from src.agent.tools import ToolContext
from src.api.deps import get_coach, get_tool_context, get_journal_store
from src.agent.tools import get_readiness_snapshot, get_training_load, get_recent_activities

templates = Jinja2Templates(directory="src/ui/templates")

router = APIRouter(include_in_schema=False)

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    tool_context: Annotated[ToolContext, Depends(get_tool_context)],
    coach: Annotated[Coach, Depends(get_coach)]
):
    today = datetime.now(timezone.utc).date()
    
    readiness = get_readiness_snapshot(tool_context, today)
    training_load = get_training_load(tool_context, today)
    recent_activities = get_recent_activities(tool_context, start=today - timedelta(days=30), end=today, limit=5)
    
    latest_insight = None
    if coach.insights:
        runs = coach.insights.list_runs(settings.user_id, limit=5)
        for run in runs:
            if run.action_id in ["readiness_check", "weekly_digest"] and run.status.value == "success":
                latest_insight = run
                break

    return templates.TemplateResponse(
        request=request,
        name="pages/dashboard.html",
        context={
            "page_title": "Waypoint — Strider Dashboard",
            "user_id": settings.user_id,
            "last_sync": datetime.now(timezone.utc),
            "readiness": readiness,
            "training_load": training_load,
            "recent_activities": recent_activities,
            "latest_insight": latest_insight,
            "unread_proposals_count": 0,
        }
    )

@router.get("/coach", response_class=HTMLResponse)
async def coach_page(
    request: Request,
    coach: Annotated[Coach, Depends(get_coach)],
    tool_context: Annotated[ToolContext, Depends(get_tool_context)]
):
    actions_catalog = coach.list_actions()
    
    today = datetime.now(timezone.utc).date()
    recent_activities = get_recent_activities(tool_context, start=today - timedelta(days=90), end=today, limit=15)
    
    activities_for_select = [
        {
            "id": act.activity_id,
            "date": act.date,
            "name": act.title or act.sport_type.value.capitalize(),
            "distance": act.distance_km
        }
        for act in recent_activities
    ]
    
    return templates.TemplateResponse(
        request=request,
        name="pages/coach.html",
        context={
            "page_title": "The Council — AI Actions",
            "actions_catalog": actions_catalog,
            "activities_for_select": activities_for_select,
            "active_result": None,
        }
    )

@router.get("/journal", response_class=HTMLResponse)
async def journal_page(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    journal: Annotated[JournalStore, Depends(get_journal_store)],
    category: Optional[str] = None
):
    from src.core.schemas import JournalCategory
    
    categories = None
    if category:
        categories = [JournalCategory(c) for c in category.split(",")]
        
    entries = journal.list_entries(
        settings.user_id,
        limit=50,
        categories=categories
    )
    
    today = datetime.now(timezone.utc).date()
    
    return templates.TemplateResponse(
        request=request,
        name="pages/journal.html",
        context={
            "page_title": "The Chronicle — Training Journal",
            "entries": entries,
            "active_filter": category or "ALL",
            "current_date": today,
        }
    )

@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    coach: Annotated[Coach, Depends(get_coach)]
):
    runs = coach.insights.list_runs(settings.user_id, limit=50) if coach.insights else []
    return templates.TemplateResponse(
        request=request,
        name="pages/history.html",
        context={
            "page_title": "Hall of Deeds — Insights History",
            "runs": runs
        }
    )
@router.get("/activities", response_class=HTMLResponse)
async def activities_list(
    request: Request,
    tool_context: Annotated[ToolContext, Depends(get_tool_context)],
    page: int = 1
):
    limit = 10
    offset = (page - 1) * limit
    activities = get_recent_activities(tool_context, limit=limit, offset=offset)
    
    has_next = len(activities) == limit
    
    return templates.TemplateResponse(
        request=request,
        name="partials/activities_table.html",
        context={
            "activities": activities,
            "page": page,
            "has_next": has_next,
            "has_prev": page > 1,
        }
    )

@router.get("/activity/{activity_id}", response_class=HTMLResponse)
async def activity_detail(
    request: Request,
    activity_id: str,
    tool_context: Annotated[ToolContext, Depends(get_tool_context)]
):
    from src.agent.tools import get_activity_summary
    activity = get_activity_summary(tool_context, activity_id)
    return templates.TemplateResponse(
        request=request,
        name="pages/activity_detail.html",
        context={
            "page_title": activity.title if activity and activity.title else "Activity Detail",
            "activity": activity,
        }
    )
