"""Messaging endpoints — with image upload, reactions, reply threading."""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update, text
from uuid import UUID

from app.database import get_db
from app.models import Conversation, ConversationParticipant, Message, User
from app.core.dependencies import get_current_active_user
from app.websocket import ws_manager

router = APIRouter()

ALLOWED_REACTIONS = {"👍", "❤️", "🚀", "🔥", "😂", "😮"}


@router.get("/conversations")
async def get_conversations(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get all conversations for current user with participant names."""
    result = await db.execute(
        select(ConversationParticipant).where(ConversationParticipant.user_id == user.id)
    )
    participants = result.scalars().all()
    conv_ids = [p.conversation_id for p in participants]

    if not conv_ids:
        return {"conversations": []}

    result = await db.execute(
        select(Conversation).where(Conversation.id.in_(conv_ids)).order_by(desc(Conversation.last_message_at))
    )
    conversations = result.scalars().all()

    enriched = []
    for conv in conversations:
        parts_result = await db.execute(
            select(ConversationParticipant).where(ConversationParticipant.conversation_id == conv.id)
        )
        parts = parts_result.scalars().all()

        other_user_id = None
        my_unread = 0
        for p in parts:
            if p.user_id != user.id:
                other_user_id = p.user_id
            else:
                my_unread = p.unread_count or 0

        other_name = conv.name or "Unknown User"
        other_avatar = conv.avatar
        other_online = False
        if other_user_id:
            other_result = await db.execute(select(User).where(User.id == other_user_id))
            other_user = other_result.scalar_one_or_none()
            if other_user:
                other_name = other_user.full_name  # Always use real name
                other_avatar = other_user.avatar
                other_online = other_user.is_online or False
            if ws_manager.is_online(str(other_user_id)):
                other_online = True

        enriched.append({
            "id": str(conv.id),
            "type": conv.type.value if hasattr(conv.type, 'value') else str(conv.type),
            "name": other_name,
            "avatar": other_avatar,
            "online": other_online,
            "last_message_content": conv.last_message_content,
            "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
            "is_active": conv.is_active,
            "other_user_id": str(other_user_id) if other_user_id else None,
            "unread_count": my_unread,
        })

    return {"conversations": enriched}


@router.post("/conversations")
async def create_conversation(data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Start a new conversation — returns existing if direct conv already exists."""
    participant_ids = data.get("participant_ids", [])
    conv_type = data.get("type", "direct")

    # For direct conversations check if one already exists
    if conv_type == "direct" and len(participant_ids) == 1:
        other_id = UUID(participant_ids[0])
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
                return {"id": str(existing_id), "message": "Conversation exists", "existing": True}

    from app.models.messaging import ConversationType
    conv = Conversation(type=ConversationType.DIRECT, name=data.get("name"))
    db.add(conv)
    await db.flush()

    all_ids = list(set([str(user.id)] + participant_ids))
    for pid in all_ids:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=UUID(pid)))

    return {"id": str(conv.id), "message": "Conversation created", "existing": False}


@router.get("/conversations/{conv_id}/messages")
async def get_messages(
    conv_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get messages in a conversation with reactions and reply data."""
    offset = (page - 1) * limit
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == UUID(conv_id))
        .order_by(Message.created_at)
        .offset(offset)
        .limit(limit)
    )
    messages = result.scalars().all()

    enriched = []
    for msg in messages:
        # Load reply preview if this is a reply
        reply_preview = None
        if msg.reply_to_id:
            rply = await db.execute(select(Message).where(Message.id == msg.reply_to_id))
            rply_msg = rply.scalar_one_or_none()
            if rply_msg:
                reply_preview = {
                    "id": str(rply_msg.id),
                    "sender_id": str(rply_msg.sender_id),
                    "content": (rply_msg.content or "")[:200],
                    "media_url": rply_msg.media_url,
                }

        enriched.append({
            "id": str(msg.id),
            "conversation_id": str(msg.conversation_id),
            "sender_id": str(msg.sender_id),
            "content": msg.content,
            "message_type": msg.message_type,
            "media_url": msg.media_url,
            "file_name": msg.file_name,
            "file_size": msg.file_size,
            "is_read": msg.is_read,
            "is_edited": msg.is_edited,
            "is_deleted": msg.is_deleted,
            "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
            "reply_preview": reply_preview,
            # New fields — safe fallback if column doesn't exist yet
            "reactions": _safe_get(msg, "reactions", {}),
            "status": _safe_get(msg, "status", "sent"),
            "mine": str(msg.sender_id) == str(user.id),
            "created_at": str(msg.created_at),
        })

    return {"messages": enriched, "page": page}


def _safe_get(obj, attr, default=None):
    """Safely get an attribute that may not exist if migration hasn't run."""
    try:
        val = getattr(obj, attr, default)
        return val if val is not None else default
    except Exception:
        return default


@router.post("/conversations/{conv_id}/messages")
async def send_message(
    conv_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message with optional reply_to, media, and link preview."""
    from app.models import Notification, NotificationType

    content = data.get("content", "")
    msg_type = data.get("message_type", data.get("type", "text"))
    media_url = data.get("media_url")
    file_name = data.get("file_name")
    reply_to_id = data.get("reply_to_id")

    msg = Message(
        conversation_id=UUID(conv_id),
        sender_id=user.id,
        content=content,
        message_type=msg_type,
        media_url=media_url,
        file_name=file_name,
        reply_to_id=UUID(reply_to_id) if reply_to_id else None,
    )
    db.add(msg)

    await db.execute(
        update(Conversation)
        .where(Conversation.id == UUID(conv_id))
        .values(
            last_message_content=(content or "📎 Attachment")[:100],
            last_message_at=msg.created_at,
        )
    )

    parts_result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == UUID(conv_id),
            ConversationParticipant.user_id != user.id,
        )
    )
    other_participants = parts_result.scalars().all()
    await db.flush()
    msg_id = str(msg.id)

    # Resolve reply preview for WS payload
    reply_preview = None
    if reply_to_id:
        rply = await db.execute(select(Message).where(Message.id == UUID(reply_to_id)))
        rply_msg = rply.scalar_one_or_none()
        if rply_msg:
            reply_preview = {
                "id": str(rply_msg.id),
                "sender_id": str(rply_msg.sender_id),
                "content": (rply_msg.content or "")[:200],
                "media_url": rply_msg.media_url,
            }

    for participant in other_participants:
        recipient_id = str(participant.user_id)
        await ws_manager.send_to_user(recipient_id, {
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
            "reply_preview": reply_preview,
            "reactions": {},
            "timestamp": str(msg.created_at) if msg.created_at else None,
        })

        # Increment unread count for recipient
        await db.execute(
            update(ConversationParticipant)
            .where(
                ConversationParticipant.conversation_id == UUID(conv_id),
                ConversationParticipant.user_id == participant.user_id,
            )
            .values(unread_count=ConversationParticipant.unread_count + 1)
        )

        db.add(Notification(
            user_id=participant.user_id,
            actor_id=user.id,
            type=NotificationType.MESSAGE,
            title=f"New message from {user.full_name}",
            body=(content or "📎 Attachment")[:100],
            data={"conversation_id": conv_id},
            action_url="/messaging",
        ))

    return {"id": msg_id, "message": "Message sent"}


@router.post("/conversations/{conv_id}/messages/{message_id}/react")
async def react_to_message(
    conv_id: str,
    message_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Add or toggle an emoji reaction on a message."""
    emoji = data.get("emoji", "")
    if emoji not in ALLOWED_REACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid emoji. Allowed: {ALLOWED_REACTIONS}")

    result = await db.execute(select(Message).where(Message.id == UUID(message_id)))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    # Get current reactions safely
    try:
        reactions = dict(msg.reactions or {})
    except Exception:
        # Column doesn't exist yet — try raw SQL
        try:
            r = await db.execute(
                text("SELECT reactions FROM messages WHERE id = :mid"),
                {"mid": message_id}
            )
            row = r.fetchone()
            reactions = dict(row[0] or {}) if row and row[0] else {}
        except Exception:
            reactions = {}

    user_id_str = str(user.id)
    if emoji not in reactions:
        reactions[emoji] = []

    if user_id_str in reactions[emoji]:
        reactions[emoji].remove(user_id_str)
        if not reactions[emoji]:
            del reactions[emoji]
        action = "removed"
    else:
        reactions[emoji].append(user_id_str)
        action = "added"

    # Update reactions in DB safely
    try:
        await db.execute(
            update(Message).where(Message.id == UUID(message_id)).values(reactions=reactions)
        )
    except Exception:
        try:
            import json
            await db.execute(
                text("UPDATE messages SET reactions = :r WHERE id = :mid"),
                {"r": json.dumps(reactions), "mid": message_id}
            )
        except Exception:
            pass

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


@router.post("/conversations/{conv_id}/read")
async def mark_conversation_read(
    conv_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset unread count when user opens a conversation."""
    await db.execute(
        update(ConversationParticipant)
        .where(
            ConversationParticipant.conversation_id == UUID(conv_id),
            ConversationParticipant.user_id == user.id,
        )
        .values(unread_count=0)
    )
    # Mark all messages in conversation as read
    await db.execute(
        update(Message)
        .where(
            Message.conversation_id == UUID(conv_id),
            Message.sender_id != user.id,
            Message.is_read == False,
        )
        .values(is_read=True, status="seen")
    )
    return {"ok": True}


@router.post("/upload-attachment")
async def upload_message_attachment(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
):
    """Upload image or file for messaging (max 20 MB)."""
    ALLOWED = {
        "image/jpeg", "image/png", "image/gif", "image/webp",
        "application/pdf",
        "application/zip", "application/x-zip-compressed",
        # Voice notes
        "audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav",
        # Video messages
        "video/mp4", "video/webm", "video/quicktime", "video/x-msvideo",
        "video/3gpp", "video/mpeg",
    }
    MAX_SIZE = 50 * 1024 * 1024  # 50MB for videos

    if file.content_type not in ALLOWED:
        raise HTTPException(status_code=400, detail="Unsupported file type. Allowed: images, videos, PDF, ZIP, audio")

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Maximum is 50 MB.")

    try:
        import cloudinary.uploader
        is_image = file.content_type.startswith("image/")
        is_audio = file.content_type.startswith("audio/")
        is_video = file.content_type.startswith("video/")
        # Cloudinary resource_type: image, video (also handles audio), raw
        resource_type = "image" if is_image else "video" if (is_video or is_audio) else "raw"
        result = cloudinary.uploader.upload(
            content,
            folder=f"gfd/messages/{user.id}",
            resource_type=resource_type,
        )
        return {
            "url":       result["secure_url"],
            "file_name": file.filename,
            "file_size": len(content),
            "file_type": file.content_type,
            "is_image":  is_image,
            "is_audio":  is_audio,
            "is_video":  is_video,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(conv_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
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
    result = await db.execute(
        select(Message).where(Message.id == UUID(message_id), Message.sender_id == user.id)
    )
    msg = result.scalar_one_or_none()
    if msg:
        msg.is_deleted = True
        msg.content = "This message was deleted"


@router.patch("/messages/{message_id}")
async def edit_message(message_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Message).where(Message.id == UUID(message_id), Message.sender_id == user.id)
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    msg.content = data.get("content", msg.content)
    msg.is_edited = True
    return {"message": "Message updated"}
