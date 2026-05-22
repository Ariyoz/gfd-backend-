"""Post and social schemas."""

from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class PostCreate(BaseModel):
    content: Optional[str] = Field(None, max_length=5000)
    post_type: str = "text"
    visibility: str = "public"
    media_urls: List[str] = []
    code_snippet: Optional[str] = None
    code_language: Optional[str] = None
    hashtags: List[str] = []
    poll_options: Optional[List[dict]] = None


class PostUpdate(BaseModel):
    content: Optional[str] = Field(None, max_length=5000)
    visibility: Optional[str] = None


class PostResponse(BaseModel):
    id: UUID
    author_id: UUID
    content: Optional[str] = None
    post_type: str
    visibility: str
    media_urls: List[str] = []
    code_snippet: Optional[str] = None
    code_language: Optional[str] = None
    hashtags: List[str] = []
    like_count: int = 0
    comment_count: int = 0
    repost_count: int = 0
    bookmark_count: int = 0
    is_edited: bool = False
    is_pinned: bool = False
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    parent_comment_id: Optional[UUID] = None


class CommentResponse(BaseModel):
    id: UUID
    post_id: UUID
    author_id: UUID
    content: str
    parent_comment_id: Optional[UUID] = None
    like_count: int = 0
    is_edited: bool = False
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PaginatedPosts(BaseModel):
    posts: List[PostResponse]
    page: int
    limit: int
    total: Optional[int] = None
    has_more: bool = False
