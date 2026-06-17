"""Messaging endpoints — upgraded with reactions, link previews, search, read receipts."""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update, text, func
from uuid import UUID
from typing import Optional

from app.database import get_db
from app.models import Conversation, ConversationParticipant, Message, User, Notification, NotificationType
from app.core.dependencies import get_current_active_user
from app.websocket import ws_manager

router = APIRouter()

# ── helpers ────────────────────────────────────────────────────────────────

def _serialize_message(msg: Message, current_user_id: str) -> dict:
    reply_preview = None
    if msg.reply_to:
        reply_preview = {
            "id": str(msg.reply_to.id),
            "sender_id": str(msg.reply_to.sender_id),
            "content": (msg.reply_to.content or "")[:200],
        }
    return {
        "id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "sender_id": str(msg.sender_id),
        "content": msg.content,
        "message_type": msg.message_type,
        "media_url": msg.media_url,
        "file_name": msg.file_name,
        "file_size": msg.file_size,
        "file_type": msg.file_type,
        "status": msg.status or "sent",
        "is_read": msg.is_read,
        "is_edited": msg.is_edited,
        "is_deleted": msg.is_deleted,
        "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
        "reply_preview": reply_preview,
        "link_preview": msg.link_preview,
        "reactions": msg.reactions or {},
        "mine": str(msg.sender_id) == current_user_id,
        "created_at": str(msg.created_at),
    }


# ── conversations ───────────────────────────────────────────────────────────

@router.get("/conversations")
async def get_conversations(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all conversations for current user with participant names and unread counts."""
    result = await db.execute(
        select(ConversationParticipant).where(ConversationParticipant.user_id == user.id)
    )
    participants = result.scalars().all()
    conv_ids = [p.conversation_id for p in participants]

    if not conv_ids:
        return {"conversations": []}

    result = await db.execute(
        select(Conversation)
        .where(Conversation.id.in_(conv_ids))
        .order_by(desc(Conversation.last_message_at))
    )
    conversations = result.scalars().all()

    enriched = []
    for conv in conversations:
        parts_result = await db.execute(
            select(ConversationParticipant).where(ConversationParticipant.conversation_id == conv.id)
        )
        parts = parts_result.scalars().all()

        other_user_id = None
        for p in parts:
            if p.user_id != user.id:
                other_user_id = p.user_id
                break

        my_participant = next((p for p in parts if p.user_id == user.id), None)
        unread = my_participant.unread_count if my_participant else 0

        other_name = conv.name or "Conversation"
        other_avatar = conv.avatar
        other_online = False
        if other_user_id:
            other_result = await db.execute(select(User).where(User.id == other_user_id))
            other_user = other_result.scalar_one_or_none()
            if other_user:
                other_name = other_user.full_name
                other_avatar = other_user.avatar
                other_online = other_user.is_online or False
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
            "unread_count": unread,
            "is_active": conv.is_active,
            "other_user_id": str(other_user_id) if other_user_id else None,
            "job_id": str(conv.job_id) if conv.job_id else None,
        })

    return {"conversations": enriched}


@router.post("/conversations")
async def create_conversation(
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Start a new conversation. Returns existing one if already present (direct)."""
    participant_ids = list(set(data.get("participant_ids", [])))
    conv_type = data.get("type", "direct")

    # For direct conversations, check if one already exists
    if conv_type == "direct" and len(participant_ids) == 1:
        other_id = UUID(participant_ids[0])
        # Find conversations where both users are participants
        my_convs = await db.execute(
            select(ConversationParticipant.conversation_id).where(ConversationParticipant.user_id == user.id)
        )
        my_conv_ids = [r[0] for r in my_convs.fetchall()]

        if my_conv_ids:
            existing = await db.execute(
                select(ConversationParticipant.conversation_id)
                .where(
                    ConversationParticipant.user_id == other_id,
                    ConversationParticipant.conversation_id.in_(my_conv_ids),
                )
            )
            existing_id = existing.scalar_one_or_none()
            if existing_id:
                return {"id": str(existing_id), "message": "Conversation already exists", "existing": True}

    conv = Conversation(
        type=conv_type,
        name=data.get("name"),
        job_id=UUID(data["job_id"]) if data.get("job_id") else None,
    )
    db.add(conv)
    await db.flush()

    all_ids = list(set([str(user.id)] + participant_ids))
    for pid in all_ids:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=UUID(pid)))

    return {"id": str(conv.id), "message": "Conversation created", "existing": False}


# ── messages ────────────────────────────────────────────────────────────────

@router.get("/conversations/{conv_id}/messages")
async def get_messages(
    conv_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get messages in a conversation, oldest first. Marks unread as delivered."""
    # Verify user is participant
    part = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == UUID(conv_id),
            ConversationParticipant.user_id == user.id,
        )
    )
    if not part.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Not a participant")

    offset = (page - 1) * limit
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == UUID(conv_id), Message.is_deleted == False)
        .order_by(Message.created_at)
        .offset(offset)
        .limit(limit)
    )
    messages = result.scalars().all()

    # Load reply_to relationships for messages that have them
    for msg in messages:
        if msg.reply_to_id:
            reply_result = await db.execute(select(Message).where(Message.id == msg.reply_to_id))
            msg.reply_to = reply_result.scalar_one_or_none()
        else:
            msg.reply_to = None

    # Mark all unread messages in this conversation from others as "seen"
    unread_ids = [
        str(m.id) for m in messages
        if m.sender_id != user.id and m.status != "seen"
    ]
    if unread_ids:
        await db.execute(
            update(Message)
            .where(
                Message.conversation_id == UUID(conv_id),
                Message.sender_id != user.id,
                Message.status != "seen",
            )
            .values(status="seen", is_read=True)
        )
        # Reset unread count for this participant
        await db.execute(
            update(ConversationParticipant)
            .where(
                ConversationParticipant.conversation_id == UUID(conv_id),
                ConversationParticipant.user_id == user.id,
            )
            .values(unread_count=0)
        )
        # Notify senders that their messages are seen
        senders = list(set(m.sender_id for m in messages if m.sender_id != user.id and m.status != "seen"))
        for sender_id in senders:
            await ws_manager.send_to_user(str(sender_id), {
                "type": "messages_seen",
                "data": {
                    "conversation_id": conv_id,
                    "seen_by": str(user.id),
                    "message_ids": unread_ids,
                },
            })

    return {
        "messages": [_serialize_message(m, str(user.id)) for m in messages],
        "page": page,
    }


@router.post("/conversations/{conv_id}/messages")
async def send_message(
    conv_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message — rich text, link preview, reply support, immediate WS delivery."""
    from app.services.link_preview import fetch_link_preview, extract_urls

    # Verify participant
    part = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == UUID(conv_id),
            ConversationParticipant.user_id == user.id,
        )
    )
    if not part.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Not a participant")

    content = data.get("content", "")
    msg_type = data.get("message_type", data.get("type", "text"))
    media_url = data.get("media_url")
    file_name = data.get("file_name")
    file_size = data.get("file_size")
    file_type = data.get("file_type")
    reply_to_id = data.get("reply_to_id")

    # Extract link preview if content has URLs
    link_preview = None
    if content and msg_type in ("text", "link"):
        urls = extract_urls(content)
        if urls:
            link_preview = await fetch_link_preview(urls[0])
            if link_preview and msg_type == "text":
                msg_type = "link"

    msg = Message(
        conversation_id=UUID(conv_id),
        sender_id=user.id,
        content=content,
        message_type=msg_type,
        media_url=media_url,
        file_name=file_name,
        file_size=file_size,
        file_type=file_type,
        reply_to_id=UUID(reply_to_id) if reply_to_id else None,
        link_preview=link_preview,
        status="sent",
        reactions={},
    )
    db.add(msg)

    # Update conversation preview
    await db.execute(
        update(Conversation)
        .where(Conversation.id == UUID(conv_id))
        .values(
            last_message_content=(content or file_name or "📎 Attachment")[:100],
            last_message_at=str(msg.created_at),
        )
    )

    # Get other participants
    parts_result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == UUID(conv_id),
            ConversationParticipant.user_id != user.id,
        )
    )
    other_participants = parts_result.scalars().all()

    await db.flush()
    msg_id = str(msg.id)

    # Resolve reply preview
    reply_preview = None
    if reply_to_id:
        rply = await db.execute(select(Message).where(Message.id == UUID(reply_to_id)))
        rply_msg = rply.scalar_one_or_none()
        if rply_msg:
            reply_preview = {
                "id": str(rply_msg.id),
                "sender_id": str(rply_msg.sender_id),
                "content": (rply_msg.content or "")[:200],
            }

    # Send WebSocket event to each recipient + increment their unread count
    for participant in other_participants:
        recipient_id = str(participant.user_id)

        # Increment unread count
        await db.execute(
            update(ConversationParticipant)
            .where(
                ConversationParticipant.conversation_id == UUID(conv_id),
                ConversationParticipant.user_id == participant.user_id,
            )
            .values(unread_count=ConversationParticipant.unread_count + 1)
        )

        # If recipient is online — mark as delivered
        if ws_manager.is_online(recipient_id):
            await db.execute(
                update(Message).where(Message.id == msg.id).values(status="delivered")
            )
            await ws_manager.send_to_user(recipient_id, {
                "type": "message_delivered_ack",
                "data": {"message_id": msg_id, "conversation_id": conv_id},
            })

        ws_payload = {
            "type": "message_sent",
            "from": str(user.id),
            "from_name": user.full_name,
            "from_avatar": user.avatar or "",
            "content": content,
            "conversation_id": conv_id,
            "message_id": msg_id,
            "message_type": msg_type,
            "media_url": media_url,
            "file_name": file_name,
            "link_preview": link_preview,
            "reply_preview": reply_preview,
            "reactions": {},
            "status": "delivered" if ws_manager.is_online(recipient_id) else "sent",
            "timestamp": str(msg.created_at),
        }
        await ws_manager.send_to_user(recipient_id, ws_payload)

        # DB notification
        db.add(Notification(
            user_id=participant.user_id,
            actor_id=user.id,
            type=NotificationType.MESSAGE,
            title=f"New message from {user.full_name}",
            body=(content or "📎 Attachment")[:100],
            data={"conversation_id": conv_id},
            action_url="/messaging",
        ))

    # Also notify sender of delivery status update via WS
    status = "delivered" if any(ws_manager.is_online(str(p.user_id)) for p in other_participants) else "sent"
    await ws_manager.send_to_user(str(user.id), {
        "type": "message_status_update",
        "data": {"message_id": msg_id, "status": status},
    })

    return {
        "id": msg_id,
        "status": status,
        "link_preview": link_preview,
        "message": "Message sent",
    }


@router.post("/conversations/{conv_id}/messages/{message_id}/reactions")
async def react_to_message(
    conv_id: str,
    message_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Add or remove an emoji reaction on a message."""
    emoji = data.get("emoji", "")
    ALLOWED = {"👍", "❤️", "🚀", "🔥", "😂", "😮"}
    if emoji not in ALLOWED:
        raise HTTPException(status_code=400, detail=f"Emoji must be one of {ALLOWED}")

    result = await db.execute(select(Message).where(Message.id == UUID(message_id)))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    reactions = dict(msg.reactions or {})
    user_id_str = str(user.id)

    if emoji not in reactions:
        reactions[emoji] = []

    if user_id_str in reactions[emoji]:
        # Toggle off
        reactions[emoji].remove(user_id_str)
        if not reactions[emoji]:
            del reactions[emoji]
        action = "removed"
    else:
        reactions[emoji].append(user_id_str)
        action = "added"

    await db.execute(
        update(Message).where(Message.id == UUID(message_id)).values(reactions=reactions)
    )

    # Broadcast to all participants
    parts_result = await db.execute(
        select(ConversationParticipant).where(ConversationParticipant.conversation_id == UUID(conv_id))
    )
    for p in parts_result.scalars().all():
        await ws_manager.send_to_user(str(p.user_id), {
            "type": "message_reaction",
            "data": {
                "message_id": message_id,
                "conversation_id": conv_id,
                "emoji": emoji,
                "reactions": reactions,
                "actor_id": user_id_str,
                "action": action,
            },
        })

    return {"reactions": reactions, "action": action}


@router.get("/conversations/{conv_id}/messages/search")
async def search_messages(
    conv_id: str,
    q: str = Query(..., min_length=1, max_length=200),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Search messages by text content within a conversation."""
    # Verify participant
    part = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == UUID(conv_id),
            ConversationParticipant.user_id == user.id,
        )
    )
    if not part.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Not a participant")

    offset = (page - 1) * limit
    result = await db.execute(
        select(Message)
        .where(
            Message.conversation_id == UUID(conv_id),
            Message.is_deleted == False,
            Message.content.ilike(f"%{q}%"),
        )
        .order_by(desc(Message.created_at))
        .offset(offset)
        .limit(limit)
    )
    messages = result.scalars().all()
    for msg in messages:
        msg.reply_to = None

    return {
        "messages": [_serialize_message(m, str(user.id)) for m in messages],
        "query": q,
        "total": len(messages),
    }


@router.patch("/messages/{message_id}/read")
async def mark_messages_read(
    message_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a specific message (and all earlier unread) as seen. Used for single-message read receipt."""
    result = await db.execute(select(Message).where(Message.id == UUID(message_id)))
    msg = result.scalar_one_or_none()
    if not msg or msg.sender_id == user.id:
        return {"message": "No action"}

    await db.execute(
        update(Message)
        .where(
            Message.id == UUID(message_id),
            Message.sender_id != user.id,
        )
        .values(status="seen", is_read=True)
    )

    # Notify sender
    await ws_manager.send_to_user(str(msg.sender_id), {
        "type": "messages_seen",
        "data": {
            "conversation_id": str(msg.conversation_id),
            "seen_by": str(user.id),
            "message_ids": [message_id],
        },
    })
    return {"message": "Marked as seen"}


@router.post("/conversations/{conv_id}/messages/{message_id}/delivered")
async def mark_message_delivered(
    conv_id: str,
    message_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Called when recipient receives a message — update to 'delivered'."""
    await db.execute(
        update(Message)
        .where(Message.id == UUID(message_id), Message.status == "sent")
        .values(status="delivered")
    )

    result = await db.execute(select(Message).where(Message.id == UUID(message_id)))
    msg = result.scalar_one_or_none()
    if msg:
        await ws_manager.send_to_user(str(msg.sender_id), {
            "type": "message_status_update",
            "data": {"message_id": message_id, "status": "delivered"},
        })
    return {"status": "delivered"}


@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(
    conv_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove current user from a conversation."""
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
async def delete_message(
    message_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a message (own messages only)."""
    result = await db.execute(
        select(Message).where(Message.id == UUID(message_id), Message.sender_id == user.id)
    )
    msg = result.scalar_one_or_none()
    if msg:
        msg.is_deleted = True
        msg.content = "This message was deleted"


@router.patch("/messages/{message_id}")
async def edit_message(
    message_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Edit a message (own messages only)."""
    result = await db.execute(
        select(Message).where(Message.id == UUID(message_id), Message.sender_id == user.id)
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    msg.content = data.get("content", msg.content)
    msg.is_edited = True
    return {"message": "Message updated"}


@router.post("/upload-attachment")
async def upload_message_attachment(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
):
    """Upload a message attachment (image, PDF, or archive up to 20 MB)."""
    ALLOWED_TYPES = {
        "image/jpeg", "image/png", "image/gif", "image/webp",
        "application/pdf",
        "application/zip", "application/x-zip-compressed",
        "application/x-rar-compressed", "application/x-7z-compressed",
    }
    MAX_SIZE = 20 * 1024 * 1024  # 20 MB

    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{file.content_type}'. Allowed: images, PDF, ZIP/RAR/7Z",
        )

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Maximum is 20 MB.")

    try:
        import cloudinary.uploader
        is_image = file.content_type.startswith("image/")
        result = cloudinary.uploader.upload(
            content,
            folder=f"gfd/messages/{user.id}",
            resource_type="image" if is_image else "raw",
        )
        return {
            "url": result["secure_url"],
            "public_id": result["public_id"],
            "file_name": file.filename,
            "file_size": len(content),
            "file_type": file.content_type,
            "is_image": is_image,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
