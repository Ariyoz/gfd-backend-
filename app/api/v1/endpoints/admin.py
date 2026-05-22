"""Admin endpoints — moderation, user management, analytics."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from uuid import UUID

from app.database import get_db
from app.models import User, Post, Project, Report, UserStatus, AuditLog
from app.core.dependencies import require_admin

router = APIRouter()


@router.get("/analytics")
async def get_analytics(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Get platform analytics."""
    users_count = (await db.execute(select(func.count()).select_from(User))).scalar()
    posts_count = (await db.execute(select(func.count()).select_from(Post))).scalar()
    projects_count = (await db.execute(select(func.count()).select_from(Project))).scalar()
    reports_count = (await db.execute(select(func.count()).select_from(Report).where(Report.status == "pending"))).scalar()

    return {
        "total_users": users_count,
        "total_posts": posts_count,
        "total_projects": projects_count,
        "pending_reports": reports_count,
    }


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    role: str = Query(None),
    status_filter: str = Query(None),
    search: str = Query(None),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all users with filtering."""
    offset = (page - 1) * limit
    query = select(User).offset(offset).limit(limit)
    if role:
        query = query.where(User.role == role)
    if status_filter:
        query = query.where(User.status == status_filter)
    if search:
        query = query.where(User.full_name.ilike(f"%{search}%") | User.email.ilike(f"%{search}%"))
    result = await db.execute(query)
    return {"users": result.scalars().all()}


@router.patch("/users/{user_id}/suspend")
async def suspend_user(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Suspend a user."""
    await db.execute(update(User).where(User.id == UUID(user_id)).values(status=UserStatus.SUSPENDED))
    db.add(AuditLog(admin_id=admin.id, action="suspend_user", target_type="user", target_id=UUID(user_id)))
    return {"message": "User suspended"}


@router.patch("/users/{user_id}/reinstate")
async def reinstate_user(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Reinstate a suspended user."""
    await db.execute(update(User).where(User.id == UUID(user_id)).values(status=UserStatus.ACTIVE))
    db.add(AuditLog(admin_id=admin.id, action="reinstate_user", target_type="user", target_id=UUID(user_id)))
    return {"message": "User reinstated"}


@router.get("/reports")
async def get_reports(
    status_filter: str = Query("pending"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get content reports."""
    query = select(Report).where(Report.status == status_filter).order_by(Report.created_at.desc())
    result = await db.execute(query)
    return {"reports": result.scalars().all()}


@router.patch("/reports/{report_id}")
async def resolve_report(report_id: str, data: dict, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Resolve a report."""
    result = await db.execute(select(Report).where(Report.id == UUID(report_id)))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    report.status = data.get("status", "resolved")
    report.resolved_by = admin.id
    return {"message": "Report updated"}
