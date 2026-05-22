"""User and profile schemas."""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class UserBase(BaseModel):
    email: EmailStr
    username: str
    full_name: str
    avatar: Optional[str] = None
    role: str


class UserResponse(UserBase):
    id: UUID
    is_verified: bool
    is_online: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserPublicProfile(BaseModel):
    id: UUID
    username: str
    full_name: str
    avatar: Optional[str] = None
    banner: Optional[str] = None
    role: str
    is_verified: bool
    is_online: bool

    class Config:
        from_attributes = True


class DeveloperProfileResponse(BaseModel):
    id: UUID
    user_id: UUID
    bio: Optional[str] = None
    location: Optional[str] = None
    experience_level: Optional[str] = None
    years_of_experience: Optional[int] = None
    hourly_rate: Optional[float] = None
    available_for_hire: bool = True
    skills: List[str] = []
    tech_stack: List[str] = []
    preferred_roles: List[str] = []
    resume_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    github_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    twitter_url: Optional[str] = None
    website_url: Optional[str] = None
    certifications: List[dict] = []

    class Config:
        from_attributes = True


class DeveloperProfileUpdate(BaseModel):
    bio: Optional[str] = None
    location: Optional[str] = None
    experience_level: Optional[str] = None
    years_of_experience: Optional[int] = None
    hourly_rate: Optional[float] = None
    available_for_hire: Optional[bool] = None
    skills: Optional[List[str]] = None
    tech_stack: Optional[List[str]] = None
    preferred_roles: Optional[List[str]] = None
    portfolio_url: Optional[str] = None
    github_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    twitter_url: Optional[str] = None
    website_url: Optional[str] = None


class ClientProfileResponse(BaseModel):
    id: UUID
    user_id: UUID
    company_name: Optional[str] = None
    company_logo: Optional[str] = None
    company_bio: Optional[str] = None
    website: Optional[str] = None
    location: Optional[str] = None
    industry: Optional[str] = None

    class Config:
        from_attributes = True


class ClientProfileUpdate(BaseModel):
    company_name: Optional[str] = None
    company_bio: Optional[str] = None
    website: Optional[str] = None
    location: Optional[str] = None
    industry: Optional[str] = None


class FollowResponse(BaseModel):
    follower_count: int
    following_count: int
    is_following: bool = False
