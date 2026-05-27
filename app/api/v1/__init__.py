"""API v1 router aggregation."""

from fastapi import APIRouter
from .endpoints import auth, users, feed, projects, messages, notifications, github, admin, uploads, explore, hire, jobs

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
api_router.include_router(users.router, prefix="/users", tags=["Users"])
api_router.include_router(feed.router, prefix="/feed", tags=["Feed & Social"])
api_router.include_router(explore.router, prefix="/explore", tags=["Explore & Discovery"])
api_router.include_router(projects.router, prefix="/projects", tags=["Projects & Hiring"])
api_router.include_router(jobs.router, prefix="/jobs", tags=["Jobs Board"])
api_router.include_router(hire.router, prefix="/hire", tags=["Direct Hiring"])
api_router.include_router(messages.router, prefix="/messages", tags=["Messaging"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
api_router.include_router(github.router, prefix="/github", tags=["GitHub Integration"])
api_router.include_router(uploads.router, prefix="/uploads", tags=["File Uploads"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin"])
