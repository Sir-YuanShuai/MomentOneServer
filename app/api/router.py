from fastapi import APIRouter

from app.api.routes.assets import router as assets_router
from app.api.routes.devices import router as devices_router
from app.api.routes.habit_goals import router as habit_goals_router
from app.api.routes.moments import router as moments_router
from app.api.routes.oauth import router as oauth_router
from app.api.routes.system import router as system_router

api_router = APIRouter()
api_router.include_router(system_router)
api_router.include_router(moments_router)
api_router.include_router(devices_router)
api_router.include_router(assets_router)
api_router.include_router(habit_goals_router)
api_router.include_router(oauth_router)
