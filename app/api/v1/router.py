from fastapi import APIRouter

from app.api.v1 import admin, analytics, auth, events, jobs, metrics, overview

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(events.router)
api_router.include_router(jobs.router)
api_router.include_router(analytics.router)
api_router.include_router(overview.router)
api_router.include_router(metrics.router)
api_router.include_router(admin.router)
