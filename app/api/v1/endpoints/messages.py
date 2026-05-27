"""Messaging endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from uuid import UUID

from app.database import get_db
from app.models import Conversation, ConversationParticipant, Message, User
from app.core.dependencies import get_current_active_user

router = APIRouter()


@router.get("/conversations")
async def get_conversations(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get all conversations for current user with participant names."""
    result = await db.execute(
        select(ConversationParticipant)
        .where(ConversationParticipant.user_id == user.id)
    )
    participants = result.scalars().all()
    conv_ids = [p.conversation_id for p in participants]

    if not conv_ids:
        return {"conversations": []}

    result = await db.execute(
        select(Conversation).where(Conversation.id.in_(conv_ids)).order_by(desc(Conversation.last_message_at))
    )
    conversations = result.scalars().all()

    # Enrich with other participant's info
    enriched = []
    for conv in conversations:
        # Get all participants in this conversation
        parts_result = await db.execute(
            select(ConversationParticipant).where(ConversationParticipant.conversation_id == conv.id)
        )
        parts = parts_result.scalars().all()

        # Find the OTHER participant (not current user)
        other_user_id = None
        for p in parts:
            if p.user_id != user.id:
                other_user_id = p.user_id
                break

        # Get other user's info
        other_name = conv.name or "Conversation"
        other_avatar = conv.avatar
        other_online = False
        if other_user_id:
            other_result = await db.execute(select(User).where(User.id == other_user_id))
            other_user = other_result.scalar_one_or_none()
            if other_user:
                other_name = other_user.full_name
                other_avatar = other_user.avatar
                # Check online: WebSocket first, then DB field
                other_online = other_user.is_online or False
            # Also check WebSocket manager (more accurate for real-time)
            from app.websocket import ws_manager
            if ws_manager.is_online(str(other_user_id)):
                other_online = True

        enriched.append({
            "id": str(conv.id),
            "type": conv.type.value if conv.type else "direct",
            "name": other_name,
            "avatar": other_avatar,
            "online": other_online,
            "last_message_content": conv.last_message_content,
            "last_message_at": conv.last_message_at,
            "is_active": conv.is_active,
            "other_user_id": str(other_user_id) if other_user_id else None,
        })

    return {"conversations": enriched}


@router.post("/conversations")
async def create_conversation(data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Start a new conversation."""
    conv = Conversation(type=data.get("type", "direct"), name=data.get("name"))
    db.add(conv)
    await db.flush()

    # Add participants
    participant_ids = data.get("participant_ids", [])
    participant_ids.append(str(user.id))
    for pid in set(participant_ids):
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=UUID(pid)))

    return {"id": str(conv.id), "message": "Conversation created"}


@router.get("/conversations/{conv_id}/messages")
async def get_messages(
    conv_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get messages in a conversation (oldest first for chat display)."""
    offset = (page - 1) * limit
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == UUID(conv_id))
        .order_by(Message.created_at)
        .offset(offset)
        .limit(limit)
    )
    return {"messages": result.scalars().all()}


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Send a message — updates conversation and sends notification."""
    from app.models import Notification, NotificationType
    from sqlalchemy import update

    msg = Message(
        conversation_id=UUID(conv_id),
        sender_id=user.id,
        content=data.get("content"),
        message_type=data.get("type", "text"),
        media_url=data.get("media_url"),
    )
    db.add(msg)

    # Update conversation last message
    await db.execute(
        update(Conversation)
        .where(Conversation.id == UUID(conv_id))
        .values(
            last_message_content=data.get("content", "")[:100],
            last_message_at=str(msg.created_at) if msg.created_at else None,
        )
    )

    # Send notification to other participants
    parts_result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == UUID(conv_id),
            ConversationParticipant.user_id != user.id,
        )
    )
    for participant in parts_result.scalars().all():
        db.add(Notification(
            user_id=participant.user_id,
            actor_id=user.id,
            type=NotificationType.MESSAGE,
            title=f"New message from {user.full_name}",
            body=data.get("content", "")[:100],
            data={"conversation_id": conv_id},
            action_url="/messaging",
        ))

    await db.flush()
    return {"id": str(msg.id), "message": "Message sent"}


@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(conv_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Delete a conversation (removes user from participants)."""
    # Remove user from conversation participants
    result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == UUID(conv_id),
            ConversationParticipant.user_id == user.id,
        )
    )
    participant = result.scalar_one_or_none()
    if participant:
        await db.delete(participant)


@router.delete("/messages/{message_id}", status_code=204)
async def delete_message(message_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Delete a message (only own messages)."""
    from sqlalchemy import update as sql_update
    result = await db.execute(
        select(Message).where(Message.id == UUID(message_id), Message.sender_id == user.id)
    )
    msg = result.scalar_one_or_none()
    if msg:
        msg.is_deleted = True
        msg.content = "This message was deleted"


@router.patch("/messages/{message_id}")
async def edit_message(message_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Edit a message (only own messages)."""
    result = await db.execute(
        select(Message).where(Message.id == UUID(message_id), Message.sender_id == user.id)
    )
    msg = result.scalar_one_or_none()
    if not msg:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Message not found")
    msg.content = data.get("content", msg.content)
    msg.is_edited = True
    return {"message": "Message updated"}
