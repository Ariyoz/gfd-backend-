"""Real-time synchronization service using Redis Pub/Sub + WebSockets."""

import json
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models import Post, Follow, User, Like, Comment
from app.websocket.events import EventType, broadcast_to_followers, broadcast_event, notify_admins
from app.services.cache import CacheService


class RealtimeService:
    """Handles real-time event broadcasting and cache invalidation."""

    @staticmethod
    async def on_post_created(db: AsyncSession, post: Post, author: User):
        """Broadcast new post to followers and update caches."""
        event_data = {
            "post_id": str(post.id),
            "author_id": str(author.id),
            "author_name": author.full_name,
            "author_avatar": author.avatar,
            "content": post.content[:200] if post.content else None,
            "post_type": post.post_type.value if post.post_type else "text",
            "created_at": str(post.created_at),
        }

        # Notify followers
        await broadcast_to_followers(db, author.id, {
            "type": EventType.POST_CREATED.value,
            "data": event_data,
        })

        # Notify admins
        await notify_admins(EventType.POST_CREATED, event_data)

        # Invalidate feed caches
        await CacheService.delete_pattern(f"feed:*")

    @staticmethod
    async def on_post_deleted(db: AsyncSession, post_id: UUID, author_id: UUID):
        """Broadcast post deletion."""
        await broadcast_event(
            EventType.POST_DELETED,
            {"post_id": str(post_id), "author_id": str(author_id)},
        )
        await CacheService.delete_pattern(f"feed:*")

    @staticmethod
    async def on_post_liked(db: AsyncSession, post: Post, liker: User):
        """Broadcast like event to post author."""
        event_data = {
            "post_id": str(post.id),
            "liker_id": str(liker.id),
            "liker_name": liker.full_name,
            "liker_avatar": liker.avatar,
            "like_count": post.like_count,
        }

        # Notify post author
        await broadcast_event(
            EventType.POST_LIKED,
            event_data,
            targets=[str(post.author_id)],
        )

        # Update admin
        await notify_admins(EventType.POST_LIKED, event_data)

    @staticmethod
    async def on_post_commented(db: AsyncSession, post: Post, commenter: User, comment_content: str):
        """Broadcast comment event."""
        event_data = {
            "post_id": str(post.id),
            "commenter_id": str(commenter.id),
            "commenter_name": commenter.full_name,
            "commenter_avatar": commenter.avatar,
            "content_preview": comment_content[:100],
            "comment_count": post.comment_count,
        }

        await broadcast_event(
            EventType.POST_COMMENTED,
            event_data,
            targets=[str(post.author_id)],
        )

    @staticmethod
    async def on_user_followed(db: AsyncSession, follower: User, followed_id: UUID):
        """Broadcast follow event and update counts."""
        # Get follower/following counts
        follower_count = (await db.execute(
            select(func.count()).where(Follow.following_id == followed_id)
        )).scalar() or 0

        following_count = (await db.execute(
            select(func.count()).where(Follow.follower_id == follower.id)
        )).scalar() or 0

        # Notify the followed user
        await broadcast_event(
            EventType.USER_FOLLOWED,
            {
                "follower_id": str(follower.id),
                "follower_name": follower.full_name,
                "follower_avatar": follower.avatar,
                "follower_count": follower_count,
            },
            targets=[str(followed_id)],
        )

        # Update follower's own count
        await broadcast_event(
            EventType.USER_FOLLOWED,
            {"following_count": following_count},
            targets=[str(follower.id)],
        )

        # Invalidate recommendation caches
        await CacheService.delete_pattern(f"recommendations:*")

    @staticmethod
    async def on_user_unfollowed(db: AsyncSession, unfollower_id: UUID, unfollowed_id: UUID):
        """Broadcast unfollow event."""
        follower_count = (await db.execute(
            select(func.count()).where(Follow.following_id == unfollowed_id)
        )).scalar() or 0

        await broadcast_event(
            EventType.USER_UNFOLLOWED,
            {"unfollower_id": str(unfollower_id), "follower_count": follower_count},
            targets=[str(unfollowed_id)],
        )

    @staticmethod
    async def on_message_sent(conversation_id: UUID, sender_id: UUID, recipient_ids: list[str], content: str):
        """Broadcast new message to conversation participants."""
        await broadcast_event(
            EventType.MESSAGE_SENT,
            {
                "conversation_id": str(conversation_id),
                "sender_id": str(sender_id),
                "content_preview": content[:100] if content else "",
            },
            targets=recipient_ids,
            exclude=str(sender_id),
        )

    @staticmethod
    async def on_profile_updated(user_id: UUID, updates: dict):
        """Broadcast profile update."""
        await broadcast_event(
            EventType.PROFILE_UPDATED,
            {"user_id": str(user_id), "updates": updates},
        )
