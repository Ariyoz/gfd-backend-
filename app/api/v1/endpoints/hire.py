"""Direct hiring endpoints — hire developers from their profile."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID

from app.database import get_db
from app.models import User, Conversation, ConversationParticipant, Message, Notification, NotificationType
from app.core.dependencies import get_current_active_user
from app.services.realtime import RealtimeService
from app.websocket.events import broadcast_event, EventType

router = APIRouter()


@router.post("/{developer_id}")
async def hire_developer(
    developer_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a hire request to a developer. Creates a conversation + notification."""
    dev_id = UUID(developer_id)

    if dev_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot hire yourself")

    # Check developer exists
    dev_result = await db.execute(select(User).where(User.id == dev_id))
    developer = dev_result.scalar_one_or_none()
    if not developer:
        raise HTTPException(status_code=404, detail="Developer not found")

    # Create or find existing conversation
    existing_conv = await db.execute(
        select(Conversation)
        .join(ConversationParticipant, ConversationParticipant.conversation_id == Conversation.id)
        .where(ConversationParticipant.user_id == user.id)
    )
    # Simple approach: create new conversation for hire request
    conv = Conversation(type="direct", name=f"Hire Request: {data.get('project_title', 'Project')}")
    db.add(conv)
    await db.flush()

    # Add participants
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=user.id))
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=dev_id))

    # Send initial hire message
    hire_message = f"🤝 **Hire Request**\n\n"
    hire_message += f"**Project:** {data.get('project_title', 'Untitled Project')}\n"
    hire_message += f"**Description:** {data.get('description', 'No description')}\n"
    if data.get('budget'):
        hire_message += f"**Budget:** {data['budget']}\n"
    if data.get('duration'):
        hire_message += f"**Duration:** {data['duration']}\n"
    hire_message += f"\n---\nSent by {user.full_name}"

    msg = Message(
        conversation_id=conv.id,
        sender_id=user.id,
        content=hire_message,
        message_type="text",
    )
    db.add(msg)

    # Create notification for developer
    notification = Notification(
        user_id=dev_id,
        actor_id=user.id,
        type=NotificationType.APPLICATION_RECEIVED,
        title=f"{user.full_name} wants to hire you!",
        body=f"Project: {data.get('project_title', 'New Project')}",
        data={"conversation_id": str(conv.id), "type": "hire_request"},
        action_url="/messaging",
    )
    db.add(notification)
    await db.flush()

    # Real-time notification
    await broadcast_event(
        EventType.NOTIFICATION,
        {
            "type": "hire_request",
            "from_name": user.full_name,
            "project_title": data.get("project_title", "New Project"),
            "conversation_id": str(conv.id),
        },
        targets=[str(dev_id)],
    )

    return {
        "message": "Hire request sent!",
        "conversation_id": str(conv.id),
    }
