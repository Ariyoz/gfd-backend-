"""Notification service — create and deliver notifications."""

from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, NotificationType
from app.websocket import ws_manager


class NotificationService:
    """Create notifications and deliver them in real-time."""

    @staticmethod
    async def create(
        db: AsyncSession,
        user_id: UUID,
        actor_id: UUID,
        type: NotificationType,
        title: str,
        body: str = None,
        data: dict = None,
        action_url: str = None,
    ) -> Notification:
        """Create a notification and push via WebSocket."""
        notification = Notification(
            user_id=user_id,
            actor_id=actor_id,
            type=type,
            title=title,
            body=body,
            data=data or {},
            action_url=action_url,
        )
        db.add(notification)
        await db.flush()

        # Push real-time via WebSocket
        await ws_manager.send_to_user(str(user_id), {
            "type": "notification",
            "data": {
                "id": str(notification.id),
                "type": type.value,
                "title": title,
                "body": body,
                "action_url": action_url,
            },
        })

        return notification

    @staticmethod
    async def notify_like(db: AsyncSession, post_author_id: UUID, liker_id: UUID, post_id: UUID, liker_name: str):
        if post_author_id == liker_id:
            return
        await NotificationService.create(
            db=db,
            user_id=post_author_id,
            actor_id=liker_id,
            type=NotificationType.LIKE,
            title=f"{liker_name} liked your post",
            data={"post_id": str(post_id)},
            action_url=f"/feed/{post_id}",
        )

    @staticmethod
    async def notify_comment(db: AsyncSession, post_author_id: UUID, commenter_id: UUID, post_id: UUID, commenter_name: str):
        if post_author_id == commenter_id:
            return
        await NotificationService.create(
            db=db,
            user_id=post_author_id,
            actor_id=commenter_id,
            type=NotificationType.COMMENT,
            title=f"{commenter_name} commented on your post",
            data={"post_id": str(post_id)},
            action_url=f"/feed/{post_id}",
        )

    @staticmethod
    async def notify_follow(db: AsyncSession, followed_id: UUID, follower_id: UUID, follower_name: str):
        await NotificationService.create(
            db=db,
            user_id=followed_id,
            actor_id=follower_id,
            type=NotificationType.FOLLOW,
            title=f"{follower_name} started following you",
            action_url=f"/developer/{follower_id}",
        )

    @staticmethod
    async def notify_application(db: AsyncSession, client_id: UUID, developer_id: UUID, project_title: str, dev_name: str):
        await NotificationService.create(
            db=db,
            user_id=client_id,
            actor_id=developer_id,
            type=NotificationType.APPLICATION_RECEIVED,
            title=f"{dev_name} applied to {project_title}",
            action_url="/dashboard/requests",
        )

    @staticmethod
    async def notify_application_status(db: AsyncSession, developer_id: UUID, client_id: UUID, project_title: str, status: str):
        ntype = NotificationType.APPLICATION_ACCEPTED if status == "accepted" else NotificationType.APPLICATION_REJECTED
        await NotificationService.create(
            db=db,
            user_id=developer_id,
            actor_id=client_id,
            type=ntype,
            title=f"Your application for {project_title} was {status}",
            action_url="/dashboard/requests",
        )

    @staticmethod
    async def notify_message(db: AsyncSession, recipient_id: UUID, sender_id: UUID, sender_name: str, conversation_id: UUID):
        await NotificationService.create(
            db=db,
            user_id=recipient_id,
            actor_id=sender_id,
            type=NotificationType.MESSAGE,
            title=f"New message from {sender_name}",
            action_url=f"/messaging",
            data={"conversation_id": str(conversation_id)},
        )

    @staticmethod
    async def notify_mention(db: AsyncSession, mentioned_id: UUID, actor_id: UUID, actor_name: str, post_id: UUID):
        await NotificationService.create(
            db=db,
            user_id=mentioned_id,
            actor_id=actor_id,
            type=NotificationType.MENTION,
            title=f"{actor_name} mentioned you in a post",
            data={"post_id": str(post_id)},
            action_url=f"/feed/{post_id}",
        )

    @staticmethod
    async def notify_job_invitation(
        db: AsyncSession,
        developer_id: UUID,
        inviter_id: UUID,
        inviter_name: str,
        job_id: str,
        job_title: str,
    ):
        await NotificationService.create(
            db=db,
            user_id=developer_id,
            actor_id=inviter_id,
            type=NotificationType.JOB_INVITATION,
            title=f"You've been invited to apply: {job_title}",
            body=f"Invited by {inviter_name}",
            data={"job_id": job_id, "inviter_id": str(inviter_id)},
            action_url=f"/jobs/{job_id}",
        )

    @staticmethod
    async def notify_application_received(
        db: AsyncSession,
        poster_id: UUID,
        applicant_id: UUID,
        applicant_name: str,
        job_title: str,
        job_id: str,
        application_id: str,
    ):
        await NotificationService.create(
            db=db,
            user_id=poster_id,
            actor_id=applicant_id,
            type=NotificationType.APPLICATION_RECEIVED,
            title=f"{applicant_name} applied to: {job_title}",
            data={"job_id": job_id, "application_id": application_id},
            action_url=f"/jobs/{job_id}/applications",
        )
