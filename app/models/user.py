"""User and profile models."""

from sqlalchemy import Column, String, Boolean, Text, Enum, ForeignKey, Integer, Float, ARRAY
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
import enum

from .base import BaseModel


class UserRole(str, enum.Enum):
    DEVELOPER = "developer"
    CLIENT = "client"
    ADMIN = "admin"


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"
    PENDING_VERIFICATION = "pending_verification"


class User(BaseModel):
    __tablename__ = "users"

    email = Column(String(255), unique=True, index=True, nullable=False)
    username = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=True)  # Null for OAuth-only users
    full_name = Column(String(150), nullable=False)
    avatar = Column(Text, nullable=True)
    banner = Column(Text, nullable=True)
    role = Column(Enum(UserRole), default=UserRole.DEVELOPER, nullable=False, index=True)
    status = Column(Enum(UserStatus), default=UserStatus.PENDING_VERIFICATION, nullable=False, index=True)
    is_verified = Column(Boolean, default=False)
    is_online = Column(Boolean, default=False)
    last_seen = Column(String(50), nullable=True)

    # Relationships
    developer_profile = relationship("DeveloperProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    client_profile = relationship("ClientProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    oauth_accounts = relationship("OAuthAccount", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    posts = relationship("Post", back_populates="author", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan", foreign_keys="[Notification.user_id]")


class DeveloperProfile(BaseModel):
    __tablename__ = "developer_profiles"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    bio = Column(Text, nullable=True)
    location = Column(String(150), nullable=True)
    experience_level = Column(String(50), nullable=True)  # junior, mid, senior, lead, principal
    years_of_experience = Column(Integer, nullable=True)
    hourly_rate = Column(Float, nullable=True)
    available_for_hire = Column(Boolean, default=True)
    skills = Column(ARRAY(String), default=[])
    tech_stack = Column(ARRAY(String), default=[])
    preferred_roles = Column(ARRAY(String), default=[])
    resume_url = Column(Text, nullable=True)
    portfolio_url = Column(Text, nullable=True)
    github_url = Column(Text, nullable=True)
    linkedin_url = Column(Text, nullable=True)
    twitter_url = Column(Text, nullable=True)
    website_url = Column(Text, nullable=True)
    certifications = Column(JSONB, default=[])

    # Relationships
    user = relationship("User", back_populates="developer_profile")
    github_profile = relationship("GitHubProfile", back_populates="developer", uselist=False, cascade="all, delete-orphan")


class ClientProfile(BaseModel):
    __tablename__ = "client_profiles"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    company_name = Column(String(200), nullable=True)
    company_logo = Column(Text, nullable=True)
    company_bio = Column(Text, nullable=True)
    website = Column(Text, nullable=True)
    location = Column(String(150), nullable=True)
    industry = Column(String(100), nullable=True)

    # Relationships
    user = relationship("User", back_populates="client_profile")
    projects = relationship("Project", back_populates="client", cascade="all, delete-orphan")


class OAuthAccount(BaseModel):
    __tablename__ = "oauth_accounts"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(50), nullable=False, index=True)  # github, google
    provider_user_id = Column(String(255), nullable=False)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_expires_at = Column(String(50), nullable=True)

    # Relationships
    user = relationship("User", back_populates="oauth_accounts")

    class Meta:
        unique_together = ("provider", "provider_user_id")


class Session(BaseModel):
    __tablename__ = "sessions"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    refresh_token = Column(Text, unique=True, nullable=False, index=True)
    user_agent = Column(Text, nullable=True)
    ip_address = Column(String(50), nullable=True)
    is_active = Column(Boolean, default=True)
    expires_at = Column(String(50), nullable=False)

    # Relationships
    user = relationship("User", back_populates="sessions")
