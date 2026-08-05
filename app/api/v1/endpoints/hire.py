"""Direct hiring endpoints — hire developers from their profile."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID

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
