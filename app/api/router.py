from fastapi import APIRouter

from app.api.routes.moments import router as moments_router
from app.api.routes.system import router as system_router

api_router = APIRouter()
api_router.include_router(system_router)
api_router.include_router(moments_router)
