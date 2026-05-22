"""Notification endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update
from uuid import UUID

from app.database import get_db
from app.models import Notification, User
from app.core.dependencies import get_current_active_user

router = APIRouter()


@router.get("/")
async def get_notifications(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    unread_only: bool = Query(False),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get user notifications."""
    offset = (page - 1) * limit
    query = select(Notification).where(Notification.user_id == user.id).order_by(desc(Notification.created_at)).offset(offset).limit(limit)
    if unread_only:
        query = query.where(Notification.is_read == False)
    result = await db.execute(query)
    return {"notifications": result.scalars().all()}


@router.get("/unread-count")
async def unread_count(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get unread notification count."""
    from sqlalchemy import func
    result = await db.execute(
        select(func.count()).where(Notification.user_id == user.id, Notification.is_read == False)
    )
    return {"count": result.scalar() or 0}


@router.patch("/{notification_id}/read")
async def mark_read(notification_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Mark notification as read."""
    await db.execute(
        update(Notification).where(Notification.id == UUID(notification_id), Notification.user_id == user.id).values(is_read=True)
    )
    return {"message": "Marked as read"}


@router.patch("/read-all")
async def mark_all_read(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Mark all notifications as read."""
    await db.execute(
        update(Notification).where(Notification.user_id == user.id, Notification.is_read == False).values(is_read=True)
    )
    return {"message": "All marked as read"}
