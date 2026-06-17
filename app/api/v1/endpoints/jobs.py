"""Jobs endpoints — upgraded with invites, real-time notifications, hiring chat initiation."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, Notification, NotificationType
from app.core.dependencies import get_current_active_user
from app.websocket import ws_manager

router = APIRouter()


@router.get("/")
async def list_jobs(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    job_type: str = Query(None),
    experience_level: str = Query(None),
    is_remote: bool = Query(None),
    search: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List open jobs with filtering."""
    from sqlalchemy import text

    offset = (page - 1) * limit

    # Build query
    where_clauses = ["j.status = 'open'"]
    params = {"limit": limit, "offset": offset}

    if job_type:
        where_clauses.append("j.job_type = :job_type")
        params["job_type"] = job_type
    if is_remote is not None:
        where_clauses.append("j.is_remote = :is_remote")
        params["is_remote"] = is_remote
    if search:
        where_clauses.append("(j.title ILIKE :search OR j.company ILIKE :search OR j.description ILIKE :search)")
        params["search"] = f"%{search}%"

    where_sql = " AND ".join(where_clauses)

    # Get jobs with poster info
    result = await db.execute(text(f"""
        SELECT j.*, u.full_name as poster_name, u.avatar as poster_avatar
        FROM jobs j
        LEFT JOIN users u ON u.id = j.poster_id
        WHERE {where_sql}
        ORDER BY j.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)
    rows = result.mappings().all()

    # Count
    count_result = await db.execute(text(f"SELECT COUNT(*) FROM jobs j WHERE {where_sql}"), params)
    total = count_result.scalar() or 0

    job_list = []
    for row in rows:
        job_list.append({
            "id": str(row["id"]),
            "title": row["title"],
            "company": row["company"] or row["poster_name"] or "Company",
            "company_logo": row.get("company_logo") or row["poster_avatar"],
            "description": row["description"] or "",
            "requirements": row.get("requirements"),
            "skills_required": row.get("skills_required") or [],
            "job_type": row.get("job_type") or "full_time",
            "experience_level": row.get("experience_level"),
            "location": row.get("location"),
            "is_remote": row.get("is_remote", True),
            "salary_min": row.get("salary_min"),
            "salary_max": row.get("salary_max"),
            "salary_currency": row.get("salary_currency") or "USD",
            "application_count": row.get("application_count") or 0,
            "view_count": row.get("view_count") or 0,
            "poster_name": row["poster_name"] or "Unknown",
            "poster_avatar": row["poster_avatar"],
            "created_at": str(row["created_at"]) if row.get("created_at") else "",
        })

    return {"jobs": job_list, "total": total, "page": page}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_job(data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Post a new job (clients/companies)."""
    try:
        from sqlalchemy import text

        # Ensure table exists (in case auto-migration didn't run)
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS jobs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                poster_id UUID NOT NULL,
                title VARCHAR(300) NOT NULL,
                company VARCHAR(200),
                description TEXT NOT NULL DEFAULT '',
                requirements TEXT,
                skills_required TEXT[] DEFAULT '{}',
                job_type VARCHAR(20) DEFAULT 'full_time',
                experience_level VARCHAR(50),
                location VARCHAR(200),
                is_remote BOOLEAN DEFAULT TRUE,
                salary_min FLOAT,
                salary_max FLOAT,
                salary_currency VARCHAR(10) DEFAULT 'USD',
                status VARCHAR(20) DEFAULT 'open',
                application_count INTEGER DEFAULT 0,
                view_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS job_applications (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                job_id UUID NOT NULL,
                applicant_id UUID NOT NULL,
                cover_letter TEXT,
                resume_url TEXT,
                portfolio_url TEXT,
                linkedin_url TEXT,
                github_url TEXT,
                years_experience INTEGER,
                expected_salary FLOAT,
                availability VARCHAR(100),
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(job_id, applicant_id)
            )
        """))

        # Insert the job
        result = await db.execute(text("""
            INSERT INTO jobs (id, poster_id, title, company, description, requirements, skills_required, job_type, experience_level, location, is_remote, salary_min, salary_max, salary_currency, status, application_count, view_count, created_at, updated_at)
            VALUES (gen_random_uuid(), :poster_id, :title, :company, :description, :requirements, :skills_required, :job_type, :experience_level, :location, :is_remote, :salary_min, :salary_max, :salary_currency, 'open', 0, 0, NOW(), NOW())
            RETURNING id
        """), {
            "poster_id": str(user.id),
            "title": data["title"],
            "company": data.get("company") or user.full_name,
            "description": data.get("description") or "",
            "requirements": data.get("requirements"),
            "skills_required": data.get("skills_required") or [],
            "job_type": data.get("job_type") or "full_time",
            "experience_level": data.get("experience_level"),
            "location": data.get("location"),
            "is_remote": data.get("is_remote", True),
            "salary_min": data.get("salary_min"),
            "salary_max": data.get("salary_max"),
            "salary_currency": data.get("salary_currency") or "USD",
        })
        row = result.fetchone()
        return {"id": str(row[0]) if row else None, "message": "Job posted successfully"}
    except Exception as e:
        print(f"[ERROR] Create job failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create job: {str(e)}")


@router.get("/my-applications")
async def get_my_applications(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all job applications submitted by the current user."""
    from sqlalchemy import text

    result = await db.execute(text("""
        SELECT ja.*, j.title as job_title, j.company, j.job_type, j.location, j.is_remote,
               u.full_name as poster_name, u.avatar as poster_avatar
        FROM job_applications ja
        JOIN jobs j ON j.id = ja.job_id
        LEFT JOIN users u ON u.id = j.poster_id
        WHERE ja.applicant_id = :user_id
        ORDER BY ja.created_at DESC
    """), {"user_id": str(user.id)})
    rows = result.mappings().all()

    return {
        "applications": [
            {
                "id": str(row["id"]),
                "job_id": str(row["job_id"]),
                "job_title": row["job_title"],
                "company": row["company"] or row["poster_name"] or "Company",
                "job_type": row.get("job_type") or "full_time",
                "location": row.get("location"),
                "is_remote": row.get("is_remote", True),
                "poster_name": row.get("poster_name"),
                "poster_avatar": row.get("poster_avatar"),
                "cover_letter": row.get("cover_letter"),
                "status": row.get("status") or "pending",
                "created_at": str(row["created_at"]) if row.get("created_at") else "",
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get("/{job_id}")
async def get_job(job_id: str, db: AsyncSession = Depends(get_db)):
    """Get job details."""
    from sqlalchemy import text

    # Increment view count
    await db.execute(text("UPDATE jobs SET view_count = view_count + 1 WHERE id = :job_id"), {"job_id": job_id})

    result = await db.execute(text("""
        SELECT j.*, u.full_name as poster_name, u.avatar as poster_avatar
        FROM jobs j LEFT JOIN users u ON u.id = j.poster_id
        WHERE j.id = :job_id
    """), {"job_id": job_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "id": str(row["id"]),
        "title": row["title"],
        "company": row["company"],
        "description": row["description"] or "",
        "requirements": row.get("requirements"),
        "skills_required": row.get("skills_required") or [],
        "job_type": row.get("job_type") or "full_time",
        "experience_level": row.get("experience_level"),
        "location": row.get("location"),
        "is_remote": row.get("is_remote", True),
        "salary_min": row.get("salary_min"),
        "salary_max": row.get("salary_max"),
        "salary_currency": row.get("salary_currency") or "USD",
        "application_count": row.get("application_count") or 0,
        "view_count": row.get("view_count") or 0,
        "poster_name": row["poster_name"] or "Unknown",
        "poster_avatar": row["poster_avatar"],
        "created_at": str(row["created_at"]) if row.get("created_at") else "",
    }


@router.post("/{job_id}/apply", status_code=status.HTTP_201_CREATED)
async def apply_to_job(job_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Apply to a job with resume, portfolio, cover letter."""
    from sqlalchemy import text

    # Check if already applied
    existing = await db.execute(text(
        "SELECT id FROM job_applications WHERE job_id = :job_id AND applicant_id = :user_id"
    ), {"job_id": job_id, "user_id": str(user.id)})
    if existing.fetchone():
        raise HTTPException(status_code=400, detail="You have already applied to this job")

    # Insert application
    result = await db.execute(text("""
        INSERT INTO job_applications (id, job_id, applicant_id, cover_letter, resume_url, portfolio_url, linkedin_url, github_url, years_experience, expected_salary, availability, status, created_at, updated_at)
        VALUES (gen_random_uuid(), :job_id, :applicant_id, :cover_letter, :resume_url, :portfolio_url, :linkedin_url, :github_url, :years_experience, :expected_salary, :availability, 'pending', NOW(), NOW())
        RETURNING id
    """), {
        "job_id": job_id,
        "applicant_id": str(user.id),
        "cover_letter": data.get("cover_letter"),
        "resume_url": data.get("resume_url"),
        "portfolio_url": data.get("portfolio_url"),
        "linkedin_url": data.get("linkedin_url"),
        "github_url": data.get("github_url"),
        "years_experience": data.get("years_experience"),
        "expected_salary": data.get("expected_salary"),
        "availability": data.get("availability"),
    })
    app_row = result.fetchone()

    # Increment application count
    await db.execute(text("UPDATE jobs SET application_count = application_count + 1 WHERE id = :job_id"), {"job_id": job_id})

    # Notify job poster
    job_result = await db.execute(text("SELECT poster_id, title FROM jobs WHERE id = :job_id"), {"job_id": job_id})
    job_row = job_result.fetchone()
    if job_row:
        db.add(Notification(
            user_id=job_row[0],
            actor_id=user.id,
            type=NotificationType.SYSTEM,
            title=f"New application from {user.full_name}",
            body=f"Applied to: {job_row[1]}",
            action_url=f"/dashboard/jobs",
        ))

    await db.flush()
    return {"id": str(app_row[0]) if app_row else None, "message": "Application submitted successfully"}


@router.get("/{job_id}/applications")
async def get_job_applications(job_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get applications for a job (only job poster can see)."""
    from sqlalchemy import text

    # Verify user is the poster
    job_check = await db.execute(text("SELECT poster_id FROM jobs WHERE id = :job_id"), {"job_id": job_id})
    job_row = job_check.fetchone()
    if not job_row or str(job_row[0]) != str(user.id):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Get applications with applicant info
    result = await db.execute(text("""
        SELECT ja.*, u.full_name as applicant_name, u.avatar as applicant_avatar
        FROM job_applications ja
        LEFT JOIN users u ON u.id = ja.applicant_id
        WHERE ja.job_id = :job_id
        ORDER BY ja.created_at DESC
    """), {"job_id": job_id})
    rows = result.mappings().all()

    app_list = []
    for row in rows:
        app_list.append({
            "id": str(row["id"]),
            "applicant_name": row["applicant_name"] or "Unknown",
            "applicant_avatar": row["applicant_avatar"],
            "applicant_id": str(row["applicant_id"]),
            "cover_letter": row.get("cover_letter"),
            "resume_url": row.get("resume_url"),
            "portfolio_url": row.get("portfolio_url"),
            "linkedin_url": row.get("linkedin_url"),
            "github_url": row.get("github_url"),
            "years_experience": row.get("years_experience"),
            "expected_salary": row.get("expected_salary"),
            "availability": row.get("availability"),
            "status": row.get("status") or "pending",
            "created_at": str(row["created_at"]) if row.get("created_at") else "",
        })

    return {"applications": app_list, "total": len(app_list)}


@router.delete("/{job_id}")
async def delete_job(job_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Delete a job posting (only poster can delete)."""
    from sqlalchemy import text
    result = await db.execute(text("DELETE FROM jobs WHERE id = :job_id AND poster_id = :user_id RETURNING id"), {"job_id": job_id, "user_id": str(user.id)})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found or not authorized")
    return {"message": "Job deleted"}


@router.patch("/{job_id}/close")
async def close_job(job_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Close a job listing — no more applications accepted."""
    from sqlalchemy import text
    result = await db.execute(text(
        "UPDATE jobs SET status = 'closed', updated_at = NOW() WHERE id = :job_id AND poster_id = :user_id RETURNING id"
    ), {"job_id": job_id, "user_id": str(user.id)})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found or not authorized")
    return {"message": "Job closed"}


@router.post("/{job_id}/invite/{developer_id}", status_code=status.HTTP_201_CREATED)
async def invite_developer(
    job_id: str,
    developer_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Invite a developer to apply for a job — sends real-time notification."""
    from sqlalchemy import text, select
    from app.models import User as UserModel

    # Verify job belongs to requester
    job_result = await db.execute(text(
        "SELECT title, poster_id FROM jobs WHERE id = :job_id AND status = 'open'"
    ), {"job_id": job_id})
    job_row = job_result.fetchone()
    if not job_row:
        raise HTTPException(status_code=404, detail="Job not found or not open")
    if str(job_row[1]) != str(user.id):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Verify developer exists
    from sqlalchemy import select as sa_select
    dev_result = await db.execute(sa_select(UserModel).where(UserModel.id == __import__("uuid").UUID(developer_id)))
    developer = dev_result.scalar_one_or_none()
    if not developer:
        raise HTTPException(status_code=404, detail="Developer not found")

    # Create invitation notification
    notification = Notification(
        user_id=developer.id,
        actor_id=user.id,
        type=NotificationType.JOB_INVITATION,
        title=f"You've been invited to apply: {job_row[0]}",
        body=f"Invited by {user.full_name}",
        data={"job_id": job_id, "inviter_id": str(user.id)},
        action_url=f"/jobs/{job_id}",
    )
    db.add(notification)
    await db.flush()

    # Real-time delivery
    await ws_manager.send_to_user(developer_id, {
        "type": "notification",
        "data": {
            "id": str(notification.id),
            "type": "job_invitation",
            "title": notification.title,
            "body": notification.body,
            "action_url": notification.action_url,
            "data": notification.data,
        },
    })

    return {"message": f"Invitation sent to {developer.full_name}"}


@router.patch("/applications/{application_id}")
async def update_application_status(application_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Update application status (shortlist, reject, accept)."""
    from sqlalchemy import text

    # Get application with job info
    result = await db.execute(text("""
        SELECT ja.*, j.poster_id, j.title as job_title
        FROM job_applications ja
        JOIN jobs j ON j.id = ja.job_id
        WHERE ja.id = :app_id
    """), {"app_id": application_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")

    if str(row["poster_id"]) != str(user.id):
        raise HTTPException(status_code=403, detail="Not authorized")

    new_status = data.get("status")
    if new_status not in ["pending", "reviewed", "shortlisted", "accepted", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    await db.execute(text(
        "UPDATE job_applications SET status = :status, updated_at = NOW() WHERE id = :app_id"
    ), {"status": new_status, "app_id": application_id})

    # Determine notification type
    if new_status == "accepted":
        ntype = NotificationType.APPLICATION_ACCEPTED
        notif_title = f"Congratulations! Your application for '{row['job_title']}' was accepted"
    elif new_status == "rejected":
        ntype = NotificationType.APPLICATION_REJECTED
        notif_title = f"Application update for '{row['job_title']}': not selected"
    else:
        ntype = NotificationType.SYSTEM
        notif_title = f"Application update: {row['job_title']} — {new_status}"

    notification = Notification(
        user_id=row["applicant_id"],
        actor_id=user.id,
        type=ntype,
        title=notif_title,
        body=f"Status: {new_status}",
        data={"job_id": str(row["job_id"]), "application_id": application_id, "status": new_status},
        action_url="/jobs/my-applications",
    )
    db.add(notification)
    await db.flush()

    # Real-time push to applicant
    await ws_manager.send_to_user(str(row["applicant_id"]), {
        "type": "notification",
        "data": {
            "id": str(notification.id),
            "type": ntype.value,
            "title": notif_title,
            "action_url": "/jobs/my-applications",
            "data": {"job_id": str(row["job_id"]), "status": new_status},
        },
    })

    return {"message": f"Application {new_status}"}


@router.post("/{job_id}/applications/{application_id}/open-chat", status_code=status.HTTP_201_CREATED)
async def open_hiring_chat(
    job_id: str,
    application_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Open a dedicated hiring conversation with the applicant from the application review."""
    from sqlalchemy import text, select as sa_select
    from app.models import Conversation, ConversationParticipant, User as UserModel
    from uuid import UUID

    # Verify job belongs to user
    job_result = await db.execute(text(
        "SELECT title, poster_id FROM jobs WHERE id = :job_id"
    ), {"job_id": job_id})
    job_row = job_result.fetchone()
    if not job_row or str(job_row[1]) != str(user.id):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Get applicant ID
    app_result = await db.execute(text(
        "SELECT applicant_id FROM job_applications WHERE id = :app_id"
    ), {"app_id": application_id})
    app_row = app_result.fetchone()
    if not app_row:
        raise HTTPException(status_code=404, detail="Application not found")

    applicant_id = app_row[0]

    # Check if a hiring conversation for this job already exists between these two users
    existing = await db.execute(sa_select(Conversation).where(
        Conversation.job_id == UUID(job_id),
        Conversation.type == "hiring",
    ))
    existing_conv = existing.scalar_one_or_none()

    if existing_conv:
        return {"conversation_id": str(existing_conv.id), "existing": True}

    # Create dedicated hiring conversation
    conv = Conversation(
        type="hiring",
        name=f"Job: {job_row[0]}",
        job_id=UUID(job_id),
    )
    db.add(conv)
    await db.flush()

    db.add(ConversationParticipant(conversation_id=conv.id, user_id=user.id))
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=UUID(str(applicant_id))))

    # Notify applicant
    notification = Notification(
        user_id=UUID(str(applicant_id)),
        actor_id=user.id,
        type=NotificationType.MESSAGE,
        title=f"{user.full_name} wants to chat about: {job_row[0]}",
        body="A hiring conversation has been started",
        data={"conversation_id": str(conv.id), "job_id": job_id},
        action_url="/messaging",
    )
    db.add(notification)
    await db.flush()

    await ws_manager.send_to_user(str(applicant_id), {
        "type": "notification",
        "data": {
            "id": str(notification.id),
            "type": "message",
            "title": notification.title,
            "action_url": "/messaging",
            "data": {"conversation_id": str(conv.id)},
        },
    })

    return {"conversation_id": str(conv.id), "existing": False}
