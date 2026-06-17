"""Messaging system models — upgraded with reactions, link previews, read receipts."""

from sqlalchemy import Column, String, Text, Boolean, ForeignKey, Integer, Enum, DateTime
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import relationship
import enum

from .base import BaseModel


class ConversationType(str, enum.Enum):
    DIRECT = "direct"
    GROUP = "group"
    HIRING = "hiring"  # Dedicated hiring conversation


class MessageStatus(str, enum.Enum):
    SENT = "sent"
    DELIVERED = "delivered"
    SEEN = "seen"


class Conversation(BaseModel):
    __tablename__ = "conversations"

    type = Column(Enum(ConversationType), default=ConversationType.DIRECT, nullable=False)
    name = Column(String(200), nullable=True)  # For group/hiring chats
    avatar = Column(Text, nullable=True)
    last_message_content = Column(Text, nullable=True)
    last_message_at = Column(String(50), nullable=True)
    is_active = Column(Boolean, default=True)
    # For hiring conversations — link back to the job
    job_id = Column(UUID(as_uuid=True), nullable=True)

    # Relationships
    participants = relationship("ConversationParticipant", back_populates="conversation", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class ConversationParticipant(BaseModel):
    __tablename__ = "conversation_participants"

    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    is_admin = Column(Boolean, default=False)
    last_read_at = Column(String(50), nullable=True)
    is_muted = Column(Boolean, default=False)
    unread_count = Column(Integer, default=0)

    # Relationships
    conversation = relationship("Conversation", back_populates="participants")


class Message(BaseModel):
    __tablename__ = "messages"

    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=True)
    # message_type: text, image, file, system, code, link
    message_type = Column(String(20), default="text")
    media_url = Column(Text, nullable=True)
    file_name = Column(String(255), nullable=True)
    file_size = Column(Integer, nullable=True)
    file_type = Column(String(50), nullable=True)  # mime type
    # Read receipt
    status = Column(String(20), default="sent")  # sent, delivered, seen
    is_read = Column(Boolean, default=False)
    is_edited = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)
    # Threading
    reply_to_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    # Link preview metadata (stored as JSONB: {url, title, description, image})
    link_preview = Column(JSONB, nullable=True)
    # Emoji reactions: {"👍": ["user_id1", ...], "❤️": [...]}
    reactions = Column(JSONB, default={})

    # Relationships
    conversation = relationship("Conversation", back_populates="messages")
    reply_to = relationship("Message", remote_side="Message.id", foreign_keys=[reply_to_id])
