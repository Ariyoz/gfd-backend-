"""Project hiring endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from uuid import UUID

from app.database import get_db
from app.models import Project, Application, User, ClientProfile, ProjectStatus, ApplicationStatus, UserRole
from app.core.dependencies import get_current_active_user, require_client

router = APIRouter()


@router.get("/")
async def list_projects(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status_filter: str = Query(None),
    skills: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List open projects with filtering."""
    offset = (page - 1) * limit
    query = select(Project).order_by(desc(Project.created_at)).offset(offset).limit(limit)
    if status_filter:
        query = query.where(Project.status == ProjectStatus(status_filter))
    else:
        query = query.where(Project.status == ProjectStatus.OPEN)
    result = await db.execute(query)
    return {"projects": result.scalars().all(), "page": page}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_project(data: dict, user: User = Depends(require_client), db: AsyncSession = Depends(get_db)):
    """Create a new project (client only)."""
    # Get client profile
    result = await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id))
    client_profile = result.scalar_one_or_none()
    if not client_profile:
        raise HTTPException(status_code=400, detail="Client profile not found")

    project = Project(
        client_id=client_profile.id,
        title=data["title"],
        description=data["description"],
        requirements=data.get("requirements"),
        skills_needed=data.get("skills_needed", []),
        budget_min=data.get("budget_min"),
        budget_max=data.get("budget_max"),
        duration=data.get("duration"),
        experience_level=data.get("experience_level"),
    )
    db.add(project)
    await db.flush()
    return {"id": str(project.id), "message": "Project created"}


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get project details."""
    result = await db.execute(select(Project).where(Project.id == UUID(project_id)))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/{project_id}/apply", status_code=status.HTTP_201_CREATED)
async def apply_to_project(project_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Apply to a project (developer)."""
    from app.models import DeveloperProfile
    dev_result = await db.execute(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
    dev_profile = dev_result.scalar_one_or_none()
    if not dev_profile:
        raise HTTPException(status_code=400, detail="Developer profile required")

    application = Application(
        project_id=UUID(project_id),
        developer_id=dev_profile.id,
        cover_letter=data.get("cover_letter"),
        proposal=data.get("proposal"),
        proposed_rate=data.get("proposed_rate"),
    )
    db.add(application)
    await db.flush()
    return {"id": str(application.id), "message": "Application submitted"}


@router.patch("/{project_id}/applications/{app_id}")
async def update_application_status(
    project_id: str, app_id: str, data: dict,
    user: User = Depends(require_client), db: AsyncSession = Depends(get_db),
):
    """Accept/reject application (client only)."""
    result = await db.execute(select(Application).where(Application.id == UUID(app_id)))
    application = result.scalar_one_or_none()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    application.status = ApplicationStatus(data["status"])
    return {"message": f"Application {data['status']}"}
