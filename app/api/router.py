from fastapi import APIRouter

from app.api.routes.admin import router as admin_router
from app.api.routes.assets import router as assets_router
from app.api.routes.devices import router as devices_router
from app.api.routes.habit_goals import router as habit_goals_router
from app.api.routes.mcp_authorizations import router as mcp_authorizations_router
from app.api.routes.mcp_discovery import router as mcp_discovery_router
from app.api.routes.mcp_oauth import router as mcp_oauth_router
from app.api.routes.moments import router as moments_router
from app.api.routes.oauth import router as oauth_router
from app.api.routes.system import router as system_router

api_router = APIRouter()
api_router.include_router(system_router)
api_router.include_router(admin_router)
api_router.include_router(moments_router)
api_router.include_router(devices_router)
api_router.include_router(assets_router)
api_router.include_router(habit_goals_router)
api_router.include_router(oauth_router)
api_router.include_router(mcp_oauth_router)
api_router.include_router(mcp_discovery_router)
api_router.include_router(mcp_authorizations_router)
