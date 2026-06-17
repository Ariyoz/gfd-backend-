"""WebSocket event types and broadcasting system — Phase 2 upgrade."""

from enum import Enum
from typing import Optional
from uuid import UUID
from app.websocket.manager import ws_manager


class EventType(str, Enum):
    # Post events
    POST_CREATED = "post_created"
    POST_UPDATED = "post_updated"
    POST_DELETED = "post_deleted"
    POST_LIKED = "post_liked"
    POST_UNLIKED = "post_unliked"
    POST_COMMENTED = "post_commented"
    POST_REPOSTED = "post_reposted"
    POST_BOOKMARKED = "post_bookmarked"

    # Follow events
    USER_FOLLOWED = "user_followed"
    USER_UNFOLLOWED = "user_unfollowed"

    # Message events
    MESSAGE_SENT = "message_sent"
    MESSAGE_READ = "message_read"
    MESSAGE_DELIVERED = "message_delivered"
    MESSAGE_SEEN = "messages_seen"
    MESSAGE_REACTION = "message_reaction"
    MESSAGE_STATUS = "message_status_update"
    TYPING_START = "typing_start"
    TYPING_STOP = "typing_stop"

    # Notification events
    NOTIFICATION = "notification"

    # Profile events
    PROFILE_UPDATED = "profile_updated"
    USER_ONLINE = "user_online"
    USER_OFFLINE = "user_offline"

    # Hiring events
    JOB_INVITATION = "job_invitation"
    APPLICATION_RECEIVED = "application_received"
    APPLICATION_STATUS = "application_status_update"

    # Admin events
    ADMIN_UPDATE = "admin_update"


async def broadcast_to_followers(db, user_id: UUID, event: dict):
    """Send event to all followers of a user."""
    from sqlalchemy import select
    from app.models import Follow

    result = await db.execute(select(Follow.follower_id).where(Follow.following_id == user_id))
    follower_ids = [str(row[0]) for row in result.fetchall()]

    for fid in follower_ids:
        await ws_manager.send_to_user(fid, event)


async def broadcast_event(
    event_type: EventType,
    data: dict,
    targets: list[str] = None,
    exclude: str = None,
):
    """Broadcast an event to specific users or all connected users."""
    event = {"type": event_type.value, "data": data}

    if targets:
        for user_id in targets:
            if user_id != exclude:
                await ws_manager.send_to_user(user_id, event)
    else:
        await ws_manager.broadcast(event, exclude=exclude)


async def notify_admins(event_type: EventType, data: dict):
    """Send ADMIN_UPDATE event to all connected users (admins filter client-side)."""
    event = {
        "type": EventType.ADMIN_UPDATE.value,
        "sub_type": event_type.value,
        "data": data,
    }
    await ws_manager.broadcast(event)
