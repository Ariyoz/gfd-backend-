"""Project and application schemas."""

from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class ProjectCreate(BaseModel):
    title: str = Field(min_length=5, max_length=300)
    description: str = Field(min_length=20)
    requirements: Optional[str] = None
    skills_needed: List[str] = []
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    budget_type: str = "fixed"
    duration: Optional[str] = None
    project_type: str = "contract"
    experience_level: Optional[str] = None
    is_remote: bool = True
    location: Optional[str] = None
    deadline: Optional[str] = None
    max_applicants: Optional[int] = None


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    skills_needed: Optional[List[str]] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    status: Optional[str] = None
    deadline: Optional[str] = None


class ProjectResponse(BaseModel):
    id: UUID
    client_id: UUID
    title: str
    description: str
    requirements: Optional[str] = None
    skills_needed: List[str] = []
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    budget_type: str
    duration: Optional[str] = None
    project_type: str
    experience_level: Optional[str] = None
    status: str
    is_remote: bool
    location: Optional[str] = None
    deadline: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ApplicationCreate(BaseModel):
    cover_letter: Optional[str] = None
    proposal: Optional[str] = None
    proposed_rate: Optional[float] = None
    proposed_duration: Optional[str] = None
    portfolio_links: List[str] = []


class ApplicationResponse(BaseModel):
    id: UUID
    project_id: UUID
    developer_id: UUID
    cover_letter: Optional[str] = None
    proposal: Optional[str] = None
    proposed_rate: Optional[float] = None
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ApplicationStatusUpdate(BaseModel):
    status: str = Field(pattern=r"^(shortlisted|accepted|rejected)$")
    client_notes: Optional[str] = None
