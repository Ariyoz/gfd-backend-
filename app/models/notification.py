"""Notification and activity models."""

from sqlalchemy import Column, String, Text, Boolean, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID, JSONB
import enum

from .base import BaseModel


class NotificationType(str, enum.Enum):
    LIKE = "like"
    COMMENT = "comment"
    FOLLOW = "follow"
    MENTION = "mention"
    REPOST = "repost"
    MESSAGE = "message"
    APPLICATION_RECEIVED = "application_received"
    APPLICATION_ACCEPTED = "application_accepted"
    APPLICATION_REJECTED = "application_rejected"
    PROJECT_UPDATE = "project_update"
    SYSTEM = "system"
    ADMIN_ALERT = "admin_alert"


class Notification(BaseModel):
    __tablename__ = "notifications"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    type = Column(Enum(NotificationType), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    is_read = Column(Boolean, default=False, index=True)
    data = Column(JSONB, default={})  # Flexible payload (post_id, project_id, etc.)
    action_url = Column(Text, nullable=True)

    # Relationships
    from sqlalchemy.orm import relationship
    user = relationship("User", back_populates="notifications", foreign_keys=[user_id])


class ActivityLog(BaseModel):
    __tablename__ = "activity_logs"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(50), nullable=True)  # user, post, project, etc.
    resource_id = Column(UUID(as_uuid=True), nullable=True)
    details = Column(JSONB, default={})
    ip_address = Column(String(50), nullable=True)
    user_agent = Column(Text, nullable=True)


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"

    admin_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(100), nullable=False)
    target_type = Column(String(50), nullable=False)
    target_id = Column(UUID(as_uuid=True), nullable=True)
    before_state = Column(JSONB, nullable=True)
    after_state = Column(JSONB, nullable=True)
    reason = Column(Text, nullable=True)
