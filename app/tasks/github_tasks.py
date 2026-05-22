"""GitHub sync background tasks."""

from .celery_app import celery_app


@celery_app.task(name="sync_github_profile")
def sync_github_profile(user_id: str):
    """Background task to sync GitHub profile and repos."""
    # This runs synchronously in Celery worker
    # Uses httpx sync client to fetch GitHub data
    import httpx
    # Implementation would mirror the sync endpoint logic
    # but run in background without blocking the API
    pass


@celery_app.task(name="scheduled_github_sync")
def scheduled_github_sync():
    """Periodic task to sync all connected GitHub accounts."""
    # Runs on a schedule (e.g., every 6 hours)
    # Iterates through all users with GitHub OAuth and syncs
    pass
