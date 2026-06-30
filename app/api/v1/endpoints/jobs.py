"""Jobs endpoints — LinkedIn-style job board (all raw SQL for compatibility)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, Notification, NotificationType
from app.core.dependencies import get_current_active_user

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
            "company_url": row.get("company_url"),
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
        from app.database import engine

        # Run DDL in autocommit mode so columns exist before INSERT
        async with engine.connect() as conn:
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS company_logo TEXT"))
            await conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS company_url TEXT"))
            await conn.commit()
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
            INSERT INTO jobs (id, poster_id, title, company, company_logo, company_url, description, requirements, skills_required, job_type, experience_level, location, is_remote, salary_min, salary_max, salary_currency, status, application_count, view_count, created_at, updated_at)
            VALUES (gen_random_uuid(), :poster_id, :title, :company, :company_logo, :company_url, :description, :requirements, :skills_required, :job_type, :experience_level, :location, :is_remote, :salary_min, :salary_max, :salary_currency, 'open', 0, 0, NOW(), NOW())
            RETURNING id
        """), {
            "poster_id": str(user.id),
            "title": data["title"],
            "company": data.get("company") or user.full_name,
            "company_logo": data.get("company_logo"),
            "company_url": data.get("company_url"),
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


# ── MUST be before /{job_id} so FastAPI matches it correctly ──
@router.get("/my-applications")
async def my_applications(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all job applications submitted by the current user."""
    from sqlalchemy import text

    result = await db.execute(text("""
        SELECT
            ja.id, ja.job_id, ja.status,
            ja.cover_letter, ja.resume_url, ja.portfolio_url,
            ja.linkedin_url, ja.github_url,
            ja.years_experience, ja.expected_salary, ja.availability,
            ja.created_at, ja.updated_at,
            j.title AS job_title, j.company AS job_company,
            j.job_type, j.location, j.salary_min, j.salary_max, j.salary_currency,
            u.full_name AS poster_name, u.avatar AS poster_avatar
        FROM job_applications ja
        JOIN jobs  j ON j.id  = ja.job_id
        JOIN users u ON u.id  = j.poster_id
        WHERE ja.applicant_id = CAST(:user_id AS UUID)
        ORDER BY ja.created_at DESC
    """), {"user_id": str(user.id)})

    rows = result.mappings().all()
    return {
        "applications": [
            {
                "id":               str(row["id"]),
                "job_id":           str(row["job_id"]),
                "status":           row["status"] or "pending",
                "cover_letter":     row["cover_letter"] or "",
                "resume_url":       row["resume_url"] or "",
                "portfolio_url":    row["portfolio_url"] or "",
                "linkedin_url":     row["linkedin_url"] or "",
                "github_url":       row["github_url"] or "",
                "years_experience": row["years_experience"],
                "expected_salary":  row["expected_salary"],
                "availability":     row["availability"] or "",
                "created_at":       str(row["created_at"]),
                "updated_at":       str(row["updated_at"]),
                "job_title":        row["job_title"] or "",
                "job_company":      row["job_company"] or "",
                "job_type":         row["job_type"] or "",
                "location":         row["location"] or "",
                "salary_min":       row["salary_min"],
                "salary_max":       row["salary_max"],
                "salary_currency":  row["salary_currency"] or "USD",
                "poster_name":      row["poster_name"] or "",
                "poster_avatar":    row["poster_avatar"] or "",
            }
            for row in rows
        ]
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

    # Verify the user is the job poster
    if str(row["poster_id"]) != str(user.id):
        raise HTTPException(status_code=403, detail="Not authorized")

    new_status = data.get("status")
    if new_status not in ["pending", "reviewed", "shortlisted", "accepted", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    # Update status
    await db.execute(text(
        "UPDATE job_applications SET status = :status, updated_at = NOW() WHERE id = :app_id"
    ), {"status": new_status, "app_id": application_id})

    # Notify the applicant
    db.add(Notification(
        user_id=row["applicant_id"],
        actor_id=user.id,
        type=NotificationType.SYSTEM,
        title=f"Application update: {row['job_title']}",
        body=f"Your application has been {new_status}",
        action_url="/jobs",
    ))

    return {"message": f"Application {new_status}"}
