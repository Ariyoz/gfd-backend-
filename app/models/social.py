"""Social/community models — posts, comments, likes, follows."""

from sqlalchemy import Column, String, Boolean, Text, Integer, ForeignKey, Enum, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import relationship
import enum

from .base import BaseModel


class PostType(str, enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    CODE = "code"
    POLL = "poll"


class PostVisibility(str, enum.Enum):
    PUBLIC = "public"
    FOLLOWERS = "followers"
    PRIVATE = "private"


class Post(BaseModel):
    __tablename__ = "posts"

    author_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    content = Column(Text, nullable=True)
    post_type = Column(Enum(PostType), default=PostType.TEXT, nullable=False)
    visibility = Column(Enum(PostVisibility), default=PostVisibility.PUBLIC, nullable=False)
    media_urls = Column(ARRAY(Text), default=[])
    code_snippet = Column(Text, nullable=True)
    code_language = Column(String(50), nullable=True)
    poll_options = Column(JSONB, nullable=True)
    hashtags = Column(ARRAY(String), default=[])
    mentions = Column(ARRAY(UUID(as_uuid=True)), default=[])
    like_count = Column(Integer, default=0)
    comment_count = Column(Integer, default=0)
    repost_count = Column(Integer, default=0)
    bookmark_count = Column(Integer, default=0)
    is_edited = Column(Boolean, default=False)
    is_pinned = Column(Boolean, default=False)
    parent_post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="SET NULL"), nullable=True)  # For reposts

    # Relationships
    author = relationship("User", back_populates="posts")
    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan")
    likes = relationship("Like", back_populates="post", cascade="all, delete-orphan")
    bookmarks = relationship("Bookmark", back_populates="post", cascade="all, delete-orphan")


class Comment(BaseModel):
    __tablename__ = "comments"

    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    parent_comment_id = Column(UUID(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)  # Nested replies
    like_count = Column(Integer, default=0)
    is_edited = Column(Boolean, default=False)

    # Relationships
    post = relationship("Post", back_populates="comments")
    replies = relationship("Comment", cascade="all, delete-orphan")


class Like(BaseModel):
    __tablename__ = "likes"
    __table_args__ = (UniqueConstraint("user_id", "post_id", name="uq_user_post_like"),)

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)

    # Relationships
    post = relationship("Post", back_populates="likes")


class Bookmark(BaseModel):
    __tablename__ = "bookmarks"
    __table_args__ = (UniqueConstraint("user_id", "post_id", name="uq_user_post_bookmark"),)

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)

    # Relationships
    post = relationship("Post", back_populates="bookmarks")


class Follow(BaseModel):
    __tablename__ = "follows"
    __table_args__ = (UniqueConstraint("follower_id", "following_id", name="uq_follow"),)

    follower_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    following_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)


class Hashtag(BaseModel):
    __tablename__ = "hashtags"

    name = Column(String(100), unique=True, nullable=False, index=True)
    post_count = Column(Integer, default=0)


class BlockedUser(BaseModel):
    __tablename__ = "blocked_users"
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id", name="uq_block"),)

    blocker_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    blocked_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)


class Report(BaseModel):
    __tablename__ = "reports"

    reporter_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reported_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reported_post_id = Column(UUID(as_uuid=True), ForeignKey("posts.id", ondelete="SET NULL"), nullable=True)
    reason = Column(String(50), nullable=False)  # spam, harassment, hate_speech, etc.
    description = Column(Text, nullable=True)
    status = Column(String(20), default="pending")  # pending, reviewed, resolved, dismissed
    resolved_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
