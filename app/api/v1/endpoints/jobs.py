"""Jobs endpoints — LinkedIn-style job board."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from uuid import UUID

from app.database import get_db
from app.models import Job, JobApplication, User, Notification, NotificationType
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


@router.get("/{job_id}")
async def get_job(job_id: str, db: AsyncSession = Depends(get_db)):
    """Get job details."""
    from sqlalchemy import update
    # Increment view count
    await db.execute(update(Job).where(Job.id == UUID(job_id)).values(view_count=Job.view_count + 1))

    result = await db.execute(select(Job).where(Job.id == UUID(job_id)))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    poster = await db.execute(select(User).where(User.id == job.poster_id))
    poster_user = poster.scalar_one_or_none()

    return {
        "id": str(job.id),
        "title": job.title,
        "company": job.company,
        "company_logo": job.company_logo,
        "description": job.description,
        "requirements": job.requirements,
        "responsibilities": job.responsibilities,
        "skills_required": job.skills_required or [],
        "job_type": job.job_type or "full_time",
        "experience_level": job.experience_level,
        "location": job.location,
        "is_remote": job.is_remote,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": job.salary_currency,
        "application_count": job.application_count or 0,
        "view_count": job.view_count or 0,
        "poster_name": poster_user.full_name if poster_user else "Unknown",
        "poster_avatar": poster_user.avatar if poster_user else None,
        "created_at": str(job.created_at),
    }


@router.post("/{job_id}/apply", status_code=status.HTTP_201_CREATED)
async def apply_to_job(job_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Apply to a job with resume, portfolio, cover letter."""
    from sqlalchemy import update

    # Check if already applied
    existing = await db.execute(
        select(JobApplication).where(JobApplication.job_id == UUID(job_id), JobApplication.applicant_id == user.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="You have already applied to this job")

    application = JobApplication(
        job_id=UUID(job_id),
        applicant_id=user.id,
        cover_letter=data.get("cover_letter"),
        resume_url=data.get("resume_url"),
        portfolio_url=data.get("portfolio_url"),
        linkedin_url=data.get("linkedin_url"),
        github_url=data.get("github_url"),
        years_experience=data.get("years_experience"),
        expected_salary=data.get("expected_salary"),
        availability=data.get("availability"),
    )
    db.add(application)

    # Increment application count
    await db.execute(update(Job).where(Job.id == UUID(job_id)).values(application_count=Job.application_count + 1))

    # Notify job poster
    job_result = await db.execute(select(Job).where(Job.id == UUID(job_id)))
    job = job_result.scalar_one_or_none()
    if job:
        db.add(Notification(
            user_id=job.poster_id,
            actor_id=user.id,
            type=NotificationType.SYSTEM,
            title=f"New application from {user.full_name}",
            body=f"Applied to: {job.title}",
            action_url=f"/dashboard/jobs",
        ))

    await db.flush()
    return {"id": str(application.id), "message": "Application submitted successfully"}


@router.get("/{job_id}/applications")
async def get_job_applications(job_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get applications for a job (only job poster can see)."""
    # Verify user is the poster
    job_result = await db.execute(select(Job).where(Job.id == UUID(job_id)))
    job = job_result.scalar_one_or_none()
    if not job or job.poster_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    result = await db.execute(
        select(JobApplication).where(JobApplication.job_id == UUID(job_id)).order_by(desc(JobApplication.created_at))
    )
    applications = result.scalars().all()

    app_list = []
    for app in applications:
        applicant = await db.execute(select(User).where(User.id == app.applicant_id))
        applicant_user = applicant.scalar_one_or_none()
        app_list.append({
            "id": str(app.id),
            "applicant_name": applicant_user.full_name if applicant_user else "Unknown",
            "applicant_avatar": applicant_user.avatar if applicant_user else None,
            "applicant_id": str(app.applicant_id),
            "cover_letter": app.cover_letter,
            "resume_url": app.resume_url,
            "portfolio_url": app.portfolio_url,
            "linkedin_url": app.linkedin_url,
            "github_url": app.github_url,
            "years_experience": app.years_experience,
            "expected_salary": app.expected_salary,
            "availability": app.availability,
            "status": app.status.value,
            "created_at": str(app.created_at),
        })

    return {"applications": app_list, "total": len(app_list)}


@router.delete("/{job_id}")
async def delete_job(job_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Delete a job posting (only poster can delete)."""
    result = await db.execute(select(Job).where(Job.id == UUID(job_id), Job.poster_id == user.id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    await db.delete(job)
    return {"message": "Job deleted"}


@router.patch("/applications/{application_id}")
async def update_application_status(application_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Update application status (shortlist, reject, accept)."""
    result = await db.execute(select(JobApplication).where(JobApplication.id == UUID(application_id)))
    application = result.scalar_one_or_none()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    # Verify the user is the job poster
    job_result = await db.execute(select(Job).where(Job.id == application.job_id))
    job = job_result.scalar_one_or_none()
    if not job or job.poster_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    new_status = data.get("status")
    if new_status not in ["pending", "reviewed", "shortlisted", "accepted", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    application.status = new_status

    # Notify the applicant
    db.add(Notification(
        user_id=application.applicant_id,
        actor_id=user.id,
        type=NotificationType.SYSTEM,
        title=f"Application update: {job.title}",
        body=f"Your application has been {new_status}",
        action_url="/jobs",
    ))

    return {"message": f"Application {new_status}"}
