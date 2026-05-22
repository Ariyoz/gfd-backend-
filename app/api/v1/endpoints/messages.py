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
    """Get all conversations for current user."""
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
    return {"conversations": result.scalars().all()}


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
    """Get messages in a conversation."""
    offset = (page - 1) * limit
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == UUID(conv_id))
        .order_by(desc(Message.created_at))
        .offset(offset)
        .limit(limit)
    )
    return {"messages": result.scalars().all()}


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Send a message."""
    msg = Message(
        conversation_id=UUID(conv_id),
        sender_id=user.id,
        content=data.get("content"),
        message_type=data.get("type", "text"),
        media_url=data.get("media_url"),
    )
    db.add(msg)
    await db.flush()
    return {"id": str(msg.id), "message": "Message sent"}
