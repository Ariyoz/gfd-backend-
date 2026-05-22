"""Notification schemas."""

from pydantic import BaseModel
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class NotificationResponse(BaseModel):
    id: UUID
    type: str
    title: str
    body: Optional[str] = None
    is_read: bool = False
    data: dict = {}
    action_url: Optional[str] = None
    actor_id: Optional[UUID] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PaginatedNotifications(BaseModel):
    notifications: List[NotificationResponse]
    unread_count: int
    page: int
    has_more: bool = False
