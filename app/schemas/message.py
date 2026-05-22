"""Messaging schemas."""

from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class ConversationCreate(BaseModel):
    type: str = "direct"
    participant_ids: List[str]
    name: Optional[str] = None  # For group chats


class ConversationResponse(BaseModel):
    id: UUID
    type: str
    name: Optional[str] = None
    avatar: Optional[str] = None
    last_message_content: Optional[str] = None
    last_message_at: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    content: Optional[str] = Field(None, max_length=5000)
    message_type: str = "text"
    media_url: Optional[str] = None
    file_name: Optional[str] = None
    reply_to_id: Optional[UUID] = None


class MessageResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_id: UUID
    content: Optional[str] = None
    message_type: str
    media_url: Optional[str] = None
    file_name: Optional[str] = None
    is_read: bool = False
    is_edited: bool = False
    reply_to_id: Optional[UUID] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TypingIndicator(BaseModel):
    conversation_id: UUID
    user_id: UUID
    is_typing: bool
