"""GitHub integration models."""

from sqlalchemy import Column, String, Integer, Boolean, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import relationship

from .base import BaseModel


class GitHubProfile(BaseModel):
    __tablename__ = "github_profiles"

    developer_id = Column(UUID(as_uuid=True), ForeignKey("developer_profiles.id", ondelete="CASCADE"), unique=True, nullable=False)
    github_id = Column(String(50), unique=True, nullable=False)
    username = Column(String(100), nullable=False, index=True)
    avatar_url = Column(Text, nullable=True)
    bio = Column(Text, nullable=True)
    followers = Column(Integer, default=0)
    following = Column(Integer, default=0)
    public_repos = Column(Integer, default=0)
    total_stars = Column(Integer, default=0)
    contribution_count = Column(Integer, default=0)
    profile_url = Column(Text, nullable=True)
    blog = Column(Text, nullable=True)
    company = Column(String(200), nullable=True)
    location = Column(String(150), nullable=True)
    hireable = Column(Boolean, nullable=True)
    social_links = Column(JSONB, default={})
    pinned_repos = Column(JSONB, default=[])
    last_synced_at = Column(String(50), nullable=True)

    # Relationships
    developer = relationship("DeveloperProfile", back_populates="github_profile")
    repositories = relationship("Repository", back_populates="github_profile", cascade="all, delete-orphan")


class Repository(BaseModel):
    __tablename__ = "repositories"

    github_profile_id = Column(UUID(as_uuid=True), ForeignKey("github_profiles.id", ondelete="CASCADE"), nullable=False)
    github_repo_id = Column(String(50), nullable=False)
    name = Column(String(200), nullable=False)
    full_name = Column(String(300), nullable=True)
    description = Column(Text, nullable=True)
    language = Column(String(50), nullable=True)
    stars = Column(Integer, default=0)
    forks = Column(Integer, default=0)
    watchers = Column(Integer, default=0)
    open_issues = Column(Integer, default=0)
    is_fork = Column(Boolean, default=False)
    is_private = Column(Boolean, default=False)
    is_featured = Column(Boolean, default=False)
    topics = Column(ARRAY(String), default=[])
    repo_url = Column(Text, nullable=False)
    homepage = Column(Text, nullable=True)
    default_branch = Column(String(100), default="main")
    last_pushed_at = Column(String(50), nullable=True)
    commit_count = Column(Integer, default=0)

    # Relationships
    github_profile = relationship("GitHubProfile", back_populates="repositories")
