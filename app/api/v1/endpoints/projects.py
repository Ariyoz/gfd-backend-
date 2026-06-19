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
    sort: str = Query("recent"),
    db: AsyncSession = Depends(get_db),
):
    """List open projects with filtering."""
    offset = (page - 1) * limit
    query = select(Project)
    if status_filter:
        query = query.where(Project.status == ProjectStatus(status_filter))

    # Sort by trending (likes + views) or recent
    if sort == "trending":
        query = query.order_by(desc(Project.like_count + Project.view_count), desc(Project.created_at))
    else:
        query = query.order_by(desc(Project.created_at))

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    projects = result.scalars().all()

    # Get total count
    from sqlalchemy import func
    count_result = await db.execute(select(func.count()).select_from(Project))
    total = count_result.scalar() or 0

    # Fetch author info for each project
    from app.models import User, ClientProfile
    project_data = []
    for p in projects:
        # Get author via client_profile -> user
        author_name = "Unknown"
        author_username = ""
        author_avatar = None
        author_id = None
        try:
            cp_result = await db.execute(select(ClientProfile).where(ClientProfile.id == p.client_id))
            cp = cp_result.scalar_one_or_none()
            if cp:
                u_result = await db.execute(select(User).where(User.id == cp.user_id))
                u = u_result.scalar_one_or_none()
                if u:
                    author_name = u.full_name
                    author_username = u.username
                    author_avatar = u.avatar
                    author_id = str(u.id)
        except Exception:
            pass

        project_data.append({
            "id": str(p.id),
            "title": p.title,
            "description": p.description,
            "requirements": p.requirements,
            "skills_needed": p.skills_needed or [],
            "budget_min": p.budget_min,
            "budget_max": p.budget_max,
            "budget_type": p.budget_type,
            "duration": p.duration,
            "project_type": p.project_type.value if p.project_type else "contract",
            "experience_level": p.experience_level,
            "status": p.status.value if p.status else "open",
            "is_remote": p.is_remote,
            "location": p.location,
            "deadline": p.deadline,
            "view_count": p.view_count or 0,
            "like_count": p.like_count or 0,
            "cover_image": p.cover_image,
            "created_at": str(p.created_at),
            # Author info
            "author_id": author_id,
            "author_name": author_name,
            "author_username": author_username,
            "author_avatar": author_avatar,
            # Project link
            "repository_url": getattr(p, "repository_url", None),
        })

    return {
        "projects": project_data,
        "total": total,
        "page": page,
    }


@router.get("/mine")
async def list_my_projects(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List projects created by the current user."""
    from sqlalchemy import func

    # Find user's client profile
    result = await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id))
    client_profile = result.scalar_one_or_none()

    if not client_profile:
        return {"projects": [], "total": 0, "page": page}

    offset = (page - 1) * limit
    query = select(Project).where(Project.client_id == client_profile.id).order_by(desc(Project.created_at)).offset(offset).limit(limit)
    result = await db.execute(query)
    projects = result.scalars().all()

    count_result = await db.execute(select(func.count()).select_from(Project).where(Project.client_id == client_profile.id))
    total = count_result.scalar() or 0

    return {
        "projects": [{
            "id": str(p.id),
            "title": p.title,
            "description": p.description,
            "skills_needed": p.skills_needed or [],
            "status": p.status.value if p.status else "open",
            "project_type": p.project_type.value if p.project_type else "contract",
            "deadline": p.deadline,
            "view_count": p.view_count or 0,
            "like_count": p.like_count or 0,
            "cover_image": p.cover_image,
            "created_at": str(p.created_at),
        } for p in projects],
        "total": total,
        "page": page,
    }


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_project(data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Create a new project (any authenticated user)."""
    # Get or create client profile for this user
    result = await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id))
    client_profile = result.scalar_one_or_none()

    if not client_profile:
        # Auto-create client profile for developers who want to post projects
        client_profile = ClientProfile(user_id=user.id)
        db.add(client_profile)
        await db.flush()

    project = Project(
        client_id=client_profile.id,
        title=data["title"],
        description=data.get("description", ""),
        requirements=data.get("requirements"),
        skills_needed=data.get("skills_needed", []),
        budget_min=data.get("budget_min"),
        budget_max=data.get("budget_max"),
        duration=data.get("duration"),
        experience_level=data.get("experience_level"),
        cover_image=data.get("cover_image"),
    )
    # Set repository_url safely (column added in Phase 2 migration)
    try:
        repo = data.get("repository_url") or data.get("github_url") or data.get("live_url")
        if repo:
            project.repository_url = repo
    except Exception:
        pass
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


@router.post("/{project_id}/like")
async def like_project(project_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Toggle like on a project — one like per user, can unlike."""
    from sqlalchemy import update, text

    # Check if user already liked this project
    check = await db.execute(text(
        "SELECT id FROM project_likes WHERE project_id = :pid AND user_id = :uid"
    ), {"pid": project_id, "uid": str(user.id)})
    existing = check.fetchone()

    if existing:
        # Unlike — remove the like
        await db.execute(text(
            "DELETE FROM project_likes WHERE project_id = :pid AND user_id = :uid"
        ), {"pid": project_id, "uid": str(user.id)})
        await db.execute(
            update(Project).where(Project.id == UUID(project_id)).values(like_count=Project.like_count - 1)
        )
        return {"message": "Project unliked", "liked": False}
    else:
        # Like — add the like
        await db.execute(text(
            "INSERT INTO project_likes (project_id, user_id) VALUES (:pid, :uid)"
        ), {"pid": project_id, "uid": str(user.id)})
        await db.execute(
            update(Project).where(Project.id == UUID(project_id)).values(like_count=Project.like_count + 1)
        )
        return {"message": "Project liked", "liked": True}


@router.post("/{project_id}/view")
async def view_project(project_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Record a project view — one view per user."""
    from sqlalchemy import update, text

    # Check if user already viewed this project
    check = await db.execute(text(
        "SELECT id FROM project_views WHERE project_id = :pid AND user_id = :uid"
    ), {"pid": project_id, "uid": str(user.id)})
    existing = check.fetchone()

    if existing:
        return {"message": "Already viewed"}

    # Record view
    await db.execute(text(
        "INSERT INTO project_views (project_id, user_id) VALUES (:pid, :uid)"
    ), {"pid": project_id, "uid": str(user.id)})
    await db.execute(
        update(Project).where(Project.id == UUID(project_id)).values(view_count=Project.view_count + 1)
    )
    return {"message": "View recorded"}
