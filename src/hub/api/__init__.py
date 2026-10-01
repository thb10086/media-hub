from fastapi import APIRouter

from hub.api import accounts, admin, channels, douyin, media, platforms, publish, system, tasks

api_router = APIRouter()
for module in (system, platforms, douyin, tasks, media, publish, accounts, channels, admin):
    api_router.include_router(module.router)

__all__ = ["api_router"]
