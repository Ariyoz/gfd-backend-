"""Project hiring system models."""

from sqlalchemy import Column, String, Text, Integer, Float, Boolean, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import relationship
import enum

from .base import BaseModel


class ProjectStatus(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ProjectType(str, enum.Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    FREELANCE = "freelance"
    INTERNSHIP = "internship"


class ApplicationStatus(str, enum.Enum):
    PENDING = "pending"
    SHORTLISTED = "shortlisted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class Project(BaseModel):
    __tablename__ = "projects"

    client_id = Column(UUID(as_uuid=True), ForeignKey("client_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(300), nullable=False)
    description = Column(Text, nullable=False)
    requirements = Column(Text, nullable=True)
    skills_needed = Column(ARRAY(String), default=[])
    budget_min = Column(Float, nullable=True)
    budget_max = Column(Float, nullable=True)
    budget_type = Column(String(20), default="fixed")  # fixed, hourly
    duration = Column(String(50), nullable=True)  # e.g., "3 months", "6 weeks"
    project_type = Column(Enum(ProjectType), default=ProjectType.CONTRACT, nullable=False)
    experience_level = Column(String(50), nullable=True)  # junior, mid, senior
    status = Column(Enum(ProjectStatus), default=ProjectStatus.OPEN, nullable=False, index=True)
    attachments = Column(JSONB, default=[])
    max_applicants = Column(Integer, nullable=True)
    is_remote = Column(Boolean, default=True)
    location = Column(String(150), nullable=True)
    deadline = Column(String(50), nullable=True)

    # Relationships
    client = relationship("ClientProfile", back_populates="projects")
    applications = relationship("Application", back_populates="project", cascade="all, delete-orphan")


class Application(BaseModel):
    __tablename__ = "applications"

    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    developer_id = Column(UUID(as_uuid=True), ForeignKey("developer_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    cover_letter = Column(Text, nullable=True)
    proposal = Column(Text, nullable=True)
    proposed_rate = Column(Float, nullable=True)
    proposed_duration = Column(String(50), nullable=True)
    portfolio_links = Column(ARRAY(Text), default=[])
    attachments = Column(JSONB, default=[])
    status = Column(Enum(ApplicationStatus), default=ApplicationStatus.PENDING, nullable=False, index=True)
    client_notes = Column(Text, nullable=True)

    # Relationships
    project = relationship("Project", back_populates="applications")
