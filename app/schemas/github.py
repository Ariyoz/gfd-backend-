"""GitHub integration schemas."""

from pydantic import BaseModel
from typing import Optional, List
from uuid import UUID


class GitHubProfileResponse(BaseModel):
    id: UUID
    github_id: str
    username: str
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    followers: int = 0
    following: int = 0
    public_repos: int = 0
    total_stars: int = 0
    contribution_count: int = 0
    profile_url: Optional[str] = None
    blog: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    hireable: Optional[bool] = None
    last_synced_at: Optional[str] = None

    class Config:
        from_attributes = True


class RepositoryResponse(BaseModel):
    id: UUID
    name: str
    full_name: Optional[str] = None
    description: Optional[str] = None
    language: Optional[str] = None
    stars: int = 0
    forks: int = 0
    watchers: int = 0
    is_fork: bool = False
    is_private: bool = False
    is_featured: bool = False
    topics: List[str] = []
    repo_url: str
    homepage: Optional[str] = None
    default_branch: str = "main"
    last_pushed_at: Optional[str] = None

    class Config:
        from_attributes = True


class GitHubSyncResponse(BaseModel):
    message: str
    repos_synced: int
    profile_updated: bool = True
