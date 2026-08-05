"""Direct hiring endpoints — hire developers from their profile."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from uuid import UUID
from typing import List

from app.database import get_db
from app.models import User, Conversation, ConversationParticipant, Message, Notification, NotificationType
from app.core.dependencies import get_current_active_user
from app.websocket.events import broadcast_event, EventType

router = APIRouter()


def _build_hire_message(data: dict, sender_name: str) -> str:
    """Build a rich, well-formatted hire request message from all form fields."""
    project_title = data.get("project_title") or "New Project"
    project_type  = data.get("project_type", "")
    description   = data.get("description", "No description provided.")
    budget        = data.get("budget", "")
    duration      = data.get("duration", "")
    skills        = data.get("skills_needed") or []
    client_name   = data.get("client_name") or sender_name
    client_email  = data.get("client_email", "")
    company       = data.get("company", "")

    lines = []
    lines.append("── Hire Request ──────────────────────")
    lines.append("")

    # Client info
    lines.append(f"From:  {client_name}")
    if company:
        lines.append(f"Company:  {company}")
    if client_email:
        lines.append(f"Email:  {client_email}")
    lines.append("")

    # Project
    lines.append(f"Project Type:  {project_type or project_title}")
    lines.append("")
    lines.append("Description:")
    lines.append(description)
    lines.append("")

    # Scope
    if budget:
        lines.append(f"Budget:  {budget}")
    if duration:
        lines.append(f"Timeline:  {duration}")
    if skills:
        lines.append(f"Tech Stack:  {', '.join(skills)}")

    lines.append("")
    lines.append("──────────────────────────────────────")
    lines.append("Please reply to discuss further details.")

    return "\n".join(lines)


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

    # Create new conversation for this hire request
    project_title = data.get("project_title") or "New Project"
    conv = Conversation(type="direct", name=f"Hire Request: {project_title}")
    db.add(conv)
    await db.flush()

    # Add both participants
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=user.id))
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=dev_id))

    # Build and send the full hire message
    hire_message = _build_hire_message(data, sender_name=user.full_name)
    msg = Message(
        conversation_id=conv.id,
        sender_id=user.id,
        content=hire_message,
        message_type="text",
    )
    db.add(msg)

    # Notify the developer
    notification = Notification(
        user_id=dev_id,
        actor_id=user.id,
        type=NotificationType.APPLICATION_RECEIVED,
        title=f"{user.full_name} wants to hire you!",
        body=f"Project: {project_title}",
        data={"conversation_id": str(conv.id), "type": "hire_request"},
        action_url="/messaging",
    )
    db.add(notification)
    await db.flush()

    # Real-time push to developer
    await broadcast_event(
        EventType.NOTIFICATION,
        {
            "type": "hire_request",
            "from_name": user.full_name,
            "project_title": project_title,
            "conversation_id": str(conv.id),
        },
        targets=[str(dev_id)],
    )

    await db.commit()

    return {
        "message": "Hire request sent!",
        "conversation_id": str(conv.id),
    }


@router.get("/requests/sent")
async def get_sent_hire_requests(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all hire requests sent BY this user (they sent the first message)."""
    # Find conversations named "Hire Request:..." where this user sent the first message
    part_q = await db.execute(
        select(ConversationParticipant.conversation_id)
        .where(ConversationParticipant.user_id == user.id)
    )
    conv_ids = [r[0] for r in part_q.fetchall()]

    if not conv_ids:
        return {"requests": []}

    conv_q = await db.execute(
        select(Conversation)
        .where(
            and_(
                Conversation.id.in_(conv_ids),
                Conversation.name.like("Hire Request:%"),
            )
        )
        .order_by(Conversation.created_at.desc())
    )
    convs = conv_q.scalars().all()

    results = []
    for conv in convs:
        # Get the first message — only include if this user SENT it
        msg_q = await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.asc())
            .limit(1)
        )
        first_msg = msg_q.scalar_one_or_none()
        if not first_msg or str(first_msg.sender_id) != str(user.id):
            continue  # skip — user didn't send this request

        # Get the other participant (developer)
        other_q = await db.execute(
            select(ConversationParticipant)
            .where(
                and_(
                    ConversationParticipant.conversation_id == conv.id,
                    ConversationParticipant.user_id != user.id,
                )
            )
        )
        other_part = other_q.scalar_one_or_none()
        dev_name = ""
        dev_avatar = None
        if other_part:
            dev_q = await db.execute(select(User).where(User.id == other_part.user_id))
            dev = dev_q.scalar_one_or_none()
            if dev:
                dev_name = dev.full_name or dev.username or ""
                dev_avatar = dev.avatar

        project_title = conv.name.replace("Hire Request: ", "", 1)

        results.append({
            "id": str(conv.id),
            "project_title": project_title,
            "developer_name": dev_name,
            "developer_avatar": dev_avatar,
            "conversation_id": str(conv.id),
            "status": "sent",
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "preview": (first_msg.content or "")[:120],
        })

    return {"requests": results}


@router.get("/requests/received")
async def get_received_hire_requests(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all hire requests received BY this user (someone else sent the first message)."""
    part_q = await db.execute(
        select(ConversationParticipant.conversation_id)
        .where(ConversationParticipant.user_id == user.id)
    )
    conv_ids = [r[0] for r in part_q.fetchall()]

    if not conv_ids:
        return {"requests": []}

    conv_q = await db.execute(
        select(Conversation)
        .where(
            and_(
                Conversation.id.in_(conv_ids),
                Conversation.name.like("Hire Request:%"),
            )
        )
        .order_by(Conversation.created_at.desc())
    )
    convs = conv_q.scalars().all()

    results = []
    for conv in convs:
        # Get the first message — only include if someone ELSE sent it
        msg_q = await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.asc())
            .limit(1)
        )
        first_msg = msg_q.scalar_one_or_none()
        if not first_msg or str(first_msg.sender_id) == str(user.id):
            continue  # skip — user sent this, not received

        # Get the sender (client)
        client_q = await db.execute(select(User).where(User.id == first_msg.sender_id))
        client = client_q.scalar_one_or_none()
        client_name = (client.full_name or client.username or "") if client else ""
        client_avatar = client.avatar if client else None

        project_title = conv.name.replace("Hire Request: ", "", 1)

        results.append({
            "id": str(conv.id),
            "project_title": project_title,
            "client_name": client_name,
            "client_avatar": client_avatar,
            "conversation_id": str(conv.id),
            "status": "pending",
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "preview": (first_msg.content or "")[:120],
        })

    return {"requests": results}
