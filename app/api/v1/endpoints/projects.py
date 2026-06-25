"""Project hiring endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from uuid import UUID

from app.database import get_db
from app.models import Project, Application, User, ClientProfile, ProjectStatus, ProjectType, ApplicationStatus, UserRole
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
    """List open projects with filtering — only admin-approved (status=open) projects."""
    offset = (page - 1) * limit
    query = select(Project).where(Project.status == ProjectStatus.OPEN)

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
            "github_url":     None,  # fetched via raw SQL below
            "live_url":       None,  # fetched via raw SQL below
        })

    # Fetch extra URL columns that may not be in ORM model (added via migration)
    if project_data:
        from sqlalchemy import text as sql_text
        pid_list = [p["id"] for p in project_data]
        try:
            url_rows = await db.execute(sql_text("""
                SELECT id::text,
                       COALESCE(github_url, '')      AS github_url,
                       COALESCE(live_url, '')         AS live_url,
                       COALESCE(repository_url, '')   AS repository_url,
                       COALESCE(cover_image, '')      AS cover_image_raw
                FROM projects
                WHERE id::text = ANY(:ids)
            """), {"ids": pid_list})
            url_map = {r[0]: (r[1], r[2], r[3], r[4]) for r in url_rows.fetchall()}
            for p in project_data:
                extra = url_map.get(p["id"], ("", "", "", ""))
                p["github_url"]     = extra[0] or ""
                p["live_url"]       = extra[1] or ""
                p["repository_url"] = extra[2] or p.get("repository_url") or extra[0] or ""
                if not p.get("cover_image"):
                    p["cover_image"] = extra[3] or ""
        except Exception:
            pass  # columns may not exist on older DB

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

    # Fetch URL columns via raw SQL (may not be in ORM model yet)
    project_list = [{
        "id": str(p.id),
        "title": p.title,
        "description": p.description,
        "skills_needed": p.skills_needed or [],
        "status": p.status.value if p.status else "open",
        "project_type": p.project_type.value if p.project_type else "contract",
        "deadline": p.deadline,
        "view_count": p.view_count or 0,
        "like_count": p.like_count or 0,
        "cover_image": p.cover_image or "",
        "created_at": str(p.created_at),
        "live_url": "",
        "github_url": "",
        "repository_url": getattr(p, "repository_url", "") or "",
    } for p in projects]

    if project_list:
        from sqlalchemy import text as sql_text
        pid_list = [p["id"] for p in project_list]
        try:
            url_rows = await db.execute(sql_text("""
                SELECT id::text,
                       COALESCE(live_url, '')        AS live_url,
                       COALESCE(github_url, '')      AS github_url,
                       COALESCE(repository_url, '')  AS repository_url
                FROM projects
                WHERE id::text = ANY(:ids)
            """), {"ids": pid_list})
            url_map = {r[0]: (r[1], r[2], r[3]) for r in url_rows.fetchall()}
            for p in project_list:
                extra = url_map.get(p["id"], ("", "", ""))
                p["live_url"]        = extra[0] or ""
                p["github_url"]      = extra[1] or ""
                p["repository_url"]  = extra[2] or p["repository_url"] or ""
        except Exception:
            pass  # columns may not exist on older DB

    return {
        "projects": project_list,
        "total": total,
        "page": page,
    }


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_project(data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Create a new project (any authenticated user)."""
    if not data.get("title"):
        raise HTTPException(status_code=400, detail="Project title is required")

    # Get or create client profile for this user
    result = await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id))
    client_profile = result.scalar_one_or_none()

    if not client_profile:
        # Auto-create client profile for any user who wants to post projects
        client_profile = ClientProfile(user_id=user.id)
        db.add(client_profile)
        await db.flush()

    try:
        # Map category string to ProjectType enum value safely
        type_map = {
            'webapp': ProjectType.CONTRACT, 'mobile': ProjectType.CONTRACT,
            'api': ProjectType.CONTRACT, 'uiux': ProjectType.CONTRACT,
            'saas': ProjectType.CONTRACT, 'opensource': ProjectType.FREELANCE,
            'full_time': ProjectType.FULL_TIME, 'part_time': ProjectType.PART_TIME,
            'contract': ProjectType.CONTRACT, 'freelance': ProjectType.FREELANCE,
            'internship': ProjectType.INTERNSHIP,
        }
        raw_type = (data.get("project_type") or data.get("category") or "contract").lower()
        project_type = type_map.get(raw_type, ProjectType.CONTRACT)

        project = Project(
            client_id=client_profile.id,
            title=data["title"].strip(),
            description=data.get("description", ""),
            requirements=data.get("requirements") or data.get("description", ""),
            skills_needed=data.get("skills_needed") or [],
            budget_min=data.get("budget_min"),
            budget_max=data.get("budget_max"),
            duration=data.get("duration"),
            experience_level=data.get("experience_level") or "mid",
            cover_image=data.get("cover_image"),
            project_type=project_type,
        )
        # Start as DRAFT — will be overridden to pending_review below
        project.status = ProjectStatus.DRAFT
        db.add(project)
        await db.flush()  # write to transaction so we have the ID

        # NOTE: status stays as DRAFT — this IS the "pending review" state.
        # The admin query and user dashboard both handle 'draft' as pending review.
        # We do NOT update to 'pending_review' because that value may not exist in the DB enum.

        # Set URL fields via raw SQL (columns added via auto-migrate at startup)
        from sqlalchemy import text
        url_fields = {
            "repository_url": data.get("repository_url") or data.get("github_url"),
            "github_url":     data.get("github_url"),
            "live_url":       data.get("live_url"),
        }
        for col, val in url_fields.items():
            if val:
                try:
                    await db.execute(
                        text(f"UPDATE projects SET {col} = :v WHERE id = :pid"),
                        {"v": val, "pid": str(project.id)}
                    )
                except Exception as url_err:
                    print(f"[WARN] Could not set {col}: {url_err}")

        print(f"[INFO] Project created: {project.id} status=pending_review user={user.id}")
        return {"id": str(project.id), "message": "Project submitted for review"}
    except Exception as e:
        print(f"[ERROR] Create project failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to publish project: {str(e)}")


# ══════════════════════════════════════════════════════════════════
# IMPORTANT: All /admin/* and named sub-routes MUST come BEFORE
# /{project_id} — FastAPI matches top-to-bottom and "admin" would
# otherwise be captured as a project_id UUID causing 404s.
# ══════════════════════════════════════════════════════════════════

@router.get("/admin/pending")
async def admin_pending_projects(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: list all projects pending review."""
    if user.role.value != "admin":
        raise HTTPException(403, "Admin only")
    from sqlalchemy import text
    rows = await db.execute(text("""
        SELECT p.id, p.title, p.description, p.project_type, p.status,
               p.cover_image, p.created_at,
               u.full_name AS author_name, u.email AS author_email, u.avatar AS author_avatar
        FROM projects p
        JOIN client_profiles cp ON cp.id = p.client_id
        JOIN users u ON u.id = cp.user_id
        WHERE p.status IN ('pending_review', 'draft')
        ORDER BY p.created_at DESC
    """))
    data = rows.mappings().all()
    return {"projects": [
        {
            "id":           str(r["id"]),
            "title":        r["title"] or "",
            "description":  r["description"] or "",
            "project_type": r["project_type"] or "contract",
            "status":       r["status"] or "pending_review",
            "cover_image":  r["cover_image"] or "",
            "created_at":   str(r["created_at"]),
            "author_name":  r["author_name"] or "",
            "author_email": r["author_email"] or "",
            "author_avatar":r["author_avatar"] or "",
        }
        for r in data
    ]}


@router.post("/admin/{project_id}/approve")
async def admin_approve_project(
    project_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: approve a pending project."""
    if user.role.value != "admin":
        raise HTTPException(403, "Admin only")
    from sqlalchemy import text
    await db.execute(
        text("UPDATE projects SET status = CAST('open' AS projectstatus) WHERE id = CAST(:pid AS UUID)"),
        {"pid": project_id},
    )
    return {"message": "Project approved and now live"}


@router.post("/admin/{project_id}/reject")
async def admin_reject_project(
    project_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: reject a project."""
    if user.role.value != "admin":
        raise HTTPException(403, "Admin only")
    from sqlalchemy import text
    reason = data.get("reason", "Does not meet platform guidelines")
    await db.execute(
        text("UPDATE projects SET status = CAST('cancelled' AS projectstatus) WHERE id = CAST(:pid AS UUID)"),
        {"pid": project_id},
    )
    return {"message": f"Project rejected: {reason}"}


@router.delete("/admin/{project_id}")
async def admin_delete_project(
    project_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: delete any project."""
    if user.role.value != "admin":
        raise HTTPException(403, "Admin only")
    result = await db.execute(select(Project).where(Project.id == UUID(project_id)))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")
    await db.delete(project)
    return {"message": "Project deleted"}


# ── Named sub-routes before wildcard /{project_id} ──

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
    """Toggle like on a project."""
    from sqlalchemy import update, text
    try:
        UUID(project_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    proj_check = await db.execute(text(
        "SELECT id FROM projects WHERE id = CAST(:pid AS UUID)"
    ), {"pid": project_id})
    if not proj_check.fetchone():
        raise HTTPException(status_code=404, detail="Project not found")
    check = await db.execute(text(
        "SELECT id FROM project_likes WHERE project_id = CAST(:pid AS UUID) AND user_id = CAST(:uid AS UUID)"
    ), {"pid": project_id, "uid": str(user.id)})
    if check.fetchone():
        await db.execute(text(
            "DELETE FROM project_likes WHERE project_id = CAST(:pid AS UUID) AND user_id = CAST(:uid AS UUID)"
        ), {"pid": project_id, "uid": str(user.id)})
        await db.execute(update(Project).where(Project.id == UUID(project_id)).values(like_count=Project.like_count - 1))
        return {"message": "Project unliked", "liked": False}
    else:
        await db.execute(text(
            "INSERT INTO project_likes (project_id, user_id) VALUES (CAST(:pid AS UUID), CAST(:uid AS UUID)) ON CONFLICT DO NOTHING"
        ), {"pid": project_id, "uid": str(user.id)})
        await db.execute(update(Project).where(Project.id == UUID(project_id)).values(like_count=Project.like_count + 1))
        return {"message": "Project liked", "liked": True}


@router.post("/{project_id}/view")
async def view_project(project_id: str, db: AsyncSession = Depends(get_db), request: Request = None):
    """Record a project view — works with or without auth."""
    from sqlalchemy import update, text
    auth_user_id = None
    try:
        from app.core.security import decode_token
        if request:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                payload = decode_token(auth_header.split(" ", 1)[1])
                if payload:
                    auth_user_id = payload.get("sub")
    except Exception:
        pass
    if auth_user_id:
        check = await db.execute(text(
            "SELECT id FROM project_views WHERE project_id = :pid AND user_id = :uid"
        ), {"pid": project_id, "uid": auth_user_id})
        if check.fetchone():
            return {"message": "Already viewed"}
        await db.execute(text(
            "INSERT INTO project_views (project_id, user_id) VALUES (:pid, :uid)"
        ), {"pid": project_id, "uid": auth_user_id})
    try:
        await db.execute(update(Project).where(Project.id == UUID(project_id)).values(view_count=Project.view_count + 1))
    except Exception:
        pass
    return {"message": "View recorded"}


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a project — owner only."""
    cp = await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id))
    client_profile = cp.scalar_one_or_none()
    if not client_profile:
        raise HTTPException(404, "Project not found")
    result = await db.execute(
        select(Project).where(Project.id == UUID(project_id), Project.client_id == client_profile.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found or not authorized")
    await db.delete(project)
    return {"message": "Project deleted"}


# ── Wildcard LAST — catches any /{project_id} ──
@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get project details."""
    result = await db.execute(select(Project).where(Project.id == UUID(project_id)))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project
