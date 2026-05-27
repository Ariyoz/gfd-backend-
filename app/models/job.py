"""Job posting system models — LinkedIn-style job board."""

from sqlalchemy import Column, String, Text, Integer, Float, Boolean, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import relationship
import enum

from .base import BaseModel


class JobType(str, enum.Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    FREELANCE = "freelance"
    INTERNSHIP = "internship"
    REMOTE = "remote"


class JobStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    FILLED = "filled"


class JobApplicationStatus(str, enum.Enum):
    PENDING = "pending"
    REVIEWED = "reviewed"
    SHORTLISTED = "shortlisted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Job(BaseModel):
    __tablename__ = "jobs"

    poster_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(300), nullable=False)
    company = Column(String(200), nullable=True)
    company_logo = Column(Text, nullable=True)
    description = Column(Text, nullable=False)
    requirements = Column(Text, nullable=True)
    responsibilities = Column(Text, nullable=True)
    skills_required = Column(ARRAY(String), default=[])
    job_type = Column(Enum(JobType), default=JobType.FULL_TIME, nullable=False)
    experience_level = Column(String(50), nullable=True)  # junior, mid, senior, lead
    location = Column(String(200), nullable=True)
    is_remote = Column(Boolean, default=True)
    salary_min = Column(Float, nullable=True)
    salary_max = Column(Float, nullable=True)
    salary_currency = Column(String(10), default="USD")
    status = Column(Enum(JobStatus), default=JobStatus.OPEN, nullable=False, index=True)
    application_count = Column(Integer, default=0)
    view_count = Column(Integer, default=0)

    # Relationships
    poster = relationship("User", backref="posted_jobs")
    applications = relationship("JobApplication", back_populates="job", cascade="all, delete-orphan")


class JobApplication(BaseModel):
    __tablename__ = "job_applications"

    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    applicant_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    cover_letter = Column(Text, nullable=True)
    resume_url = Column(Text, nullable=True)
    portfolio_url = Column(Text, nullable=True)
    linkedin_url = Column(Text, nullable=True)
    github_url = Column(Text, nullable=True)
    years_experience = Column(Integer, nullable=True)
    expected_salary = Column(Float, nullable=True)
    availability = Column(String(100), nullable=True)  # "immediately", "2 weeks", "1 month"
    status = Column(Enum(JobApplicationStatus), default=JobApplicationStatus.PENDING, nullable=False)

    # Relationships
    job = relationship("Job", back_populates="applications")
    applicant = relationship("User", backref="job_applications")
