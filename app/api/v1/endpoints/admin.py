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
    from app.models import UserRole, UserStatus, Conversation, Message

    users_count = (await db.execute(select(func.count()).select_from(User))).scalar()
    posts_count = (await db.execute(select(func.count()).select_from(Post))).scalar()
    projects_count = (await db.execute(select(func.count()).select_from(Project))).scalar()
    reports_count = (await db.execute(select(func.count()).select_from(Report).where(Report.status == "pending"))).scalar()

    # Additional stats
    developers_count = (await db.execute(select(func.count()).select_from(User).where(User.role == UserRole.DEVELOPER, User.status == UserStatus.ACTIVE))).scalar()
    clients_count = (await db.execute(select(func.count()).select_from(User).where(User.role == UserRole.CLIENT, User.status == UserStatus.ACTIVE))).scalar()
    suspended_count = (await db.execute(select(func.count()).select_from(User).where(User.status == UserStatus.SUSPENDED))).scalar()
    verified_count = (await db.execute(select(func.count()).select_from(User).where(User.is_verified == True))).scalar()

    # Messages count
    try:
        messages_count = (await db.execute(select(func.count()).select_from(Message))).scalar()
    except:
        messages_count = 0

    # Subscription stats
    try:
        from sqlalchemy import text as raw_text
        pending_subs = (await db.execute(raw_text("SELECT COUNT(*) FROM subscriptions WHERE status = 'pending'"))).scalar() or 0
        active_subs = (await db.execute(raw_text("SELECT COUNT(*) FROM subscriptions WHERE status = 'active'"))).scalar() or 0
    except:
        pending_subs = 0
        active_subs = 0

    return {
        "total_users": users_count,
        "total_posts": posts_count,
        "total_projects": projects_count,
        "pending_reports": reports_count,
        "developers": developers_count,
        "clients": clients_count,
        "suspended_users": suspended_count,
        "verified_users": verified_count,
        "total_messages": messages_count,
        "pending_subscriptions": pending_subs,
        "active_subscriptions": active_subs,
        # camelCase aliases for frontend compatibility
        "totalUsers": users_count,
        "totalPosts": posts_count,
        "totalProjects": projects_count,
        "pendingReports": reports_count,
        "suspendedUsers": suspended_count,
        "verifiedUsers": verified_count,
        "pendingSubscriptions": pending_subs,
        "activeSubscriptions": active_subs,
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
async def suspend_user(user_id: str, data: dict = {}, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Suspend a user. duration_hours=0 means indefinite — wipes all their content."""
    from datetime import datetime, timezone, timedelta

    duration_hours = data.get("duration_hours", 0)
    reason = data.get("reason", "")
    is_indefinite = (duration_hours == 0)

    await db.execute(update(User).where(User.id == UUID(user_id)).values(status=UserStatus.SUSPENDED))

    # If indefinite, delete ALL user content
    if is_indefinite:
        from app.models import Post, Comment, Like, Bookmark, Project, Application, ClientProfile, Notification
        from app.models import ConversationParticipant

        # Delete posts (cascade deletes comments, likes, bookmarks via FK)
        await db.execute(
            __import__("sqlalchemy").delete(Post).where(Post.author_id == UUID(user_id))
        )
        # Delete their projects (cascade deletes applications)
        cp_result = await db.execute(
            select(ClientProfile).where(ClientProfile.user_id == UUID(user_id))
        )
        cp = cp_result.scalar_one_or_none()
        if cp:
            await db.execute(
                __import__("sqlalchemy").delete(Project).where(Project.client_id == cp.id)
            )
        # Delete notifications
        await db.execute(
            __import__("sqlalchemy").delete(Notification).where(Notification.user_id == UUID(user_id))
        )

    db.add(AuditLog(
        admin_id=admin.id,
        action="suspend_user_indefinite" if is_indefinite else "suspend_user",
        target_type="user",
        target_id=UUID(user_id),
        reason=reason or None,
        after_state={
            "duration_hours": duration_hours,
            "content_deleted": is_indefinite,
            "suspended_until": (
                (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()
                if duration_hours > 0 else "indefinite"
            ),
        }
    ))
    return {
        "message": "User suspended" + (" and all content deleted" if is_indefinite else ""),
        "duration_hours": duration_hours,
        "content_deleted": is_indefinite,
    }


# ── Admin: delete any user permanently ────────────────────────────────────────

@router.delete("/users/{user_id}/delete")
async def admin_delete_user(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Permanently delete a user and ALL their content (posts, projects, messages, etc.)."""
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user_to_delete = result.scalar_one_or_none()
    if not user_to_delete:
        raise HTTPException(status_code=404, detail="User not found")

    db.add(AuditLog(
        admin_id=admin.id,
        action="delete_user",
        target_type="user",
        target_id=UUID(user_id),
        before_state={"email": user_to_delete.email, "name": user_to_delete.full_name},
    ))
    await db.delete(user_to_delete)  # CASCADE handles all related records
    return {"message": "User permanently deleted"}


# ── Admin: delete any post ────────────────────────────────────────────────────

@router.delete("/content/post/{post_id}")
async def admin_delete_post(post_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Admin: delete any user's post and all its comments/reactions."""
    from app.models import Post
    result = await db.execute(select(Post).where(Post.id == UUID(post_id)))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    db.add(AuditLog(admin_id=admin.id, action="delete_post", target_type="post", target_id=UUID(post_id)))
    await db.delete(post)
    return {"message": "Post deleted"}


# ── Admin: delete any project ─────────────────────────────────────────────────

@router.delete("/content/project/{project_id}")
async def admin_delete_project(project_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Admin: delete any user's project and all its applications."""
    from app.models import Project
    result = await db.execute(select(Project).where(Project.id == UUID(project_id)))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    db.add(AuditLog(admin_id=admin.id, action="delete_project", target_type="project", target_id=UUID(project_id)))
    await db.delete(project)
    return {"message": "Project deleted"}


# ── Admin: delete any job ─────────────────────────────────────────────────────

@router.delete("/content/job/{job_id}")
async def admin_delete_job(job_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Admin: delete any user's job posting."""
    from sqlalchemy import text as sql_text
    await db.execute(sql_text("DELETE FROM jobs WHERE id = :jid"), {"jid": job_id})
    db.add(AuditLog(admin_id=admin.id, action="delete_job", target_type="job"))
    return {"message": "Job deleted"}


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


@router.patch("/users/{user_id}/role")
async def update_user_role(user_id: str, data: dict, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Update a user's role."""
    from app.models import UserRole
    new_role = data.get("role")
    if new_role not in [r.value for r in UserRole]:
        raise HTTPException(status_code=400, detail="Invalid role")
    await db.execute(update(User).where(User.id == UUID(user_id)).values(role=new_role))
    db.add(AuditLog(admin_id=admin.id, action="change_role", target_type="user", target_id=UUID(user_id)))
    return {"message": f"User role updated to {new_role}"}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Delete a user permanently."""
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user_to_delete = result.scalar_one_or_none()
    if not user_to_delete:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user_to_delete)
    db.add(AuditLog(admin_id=admin.id, action="delete_user", target_type="user", target_id=UUID(user_id)))
    return {"message": "User deleted"}


# ── Subscription Management ──

@router.get("/subscriptions")
async def get_all_subscriptions(
    status_filter: str = Query("pending"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get all subscriptions with user info."""
    from sqlalchemy import text
    try:
        result = await db.execute(text("""
            SELECT s.*, u.full_name, u.email, u.username, u.avatar, u.is_verified
            FROM subscriptions s
            LEFT JOIN users u ON u.id = s.user_id
            WHERE s.status = :status
            ORDER BY s.created_at DESC
        """), {"status": status_filter})
        rows = result.mappings().all()
    except Exception as e:
        print(f"[WARN] Admin subscriptions fetch: {e}")
        return {"subscriptions": [], "total": 0}

    subs = []
    for row in rows:
        subs.append({
            "id": str(row["id"]),
            "user_id": str(row["user_id"]),
            "user_name": row["full_name"] or "Unknown",
            "user_email": row["email"] or "",
            "username": row.get("payment_reference") or row.get("username") or "",
            "user_avatar": row["avatar"],
            "is_verified": row["is_verified"],
            "plan": row["plan"],
            "billing_cycle": row["billing_cycle"],
            "status": row["status"],
            "started_at": str(row["started_at"]) if row["started_at"] else "",
            "expires_at": str(row["expires_at"]) if row["expires_at"] else "",
            "created_at": str(row.get("created_at", "")),
        })

    return {"subscriptions": subs, "total": len(subs)}


@router.patch("/subscriptions/{sub_id}/approve")
async def approve_subscription(sub_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Approve a subscription and grant verified badge."""
    from sqlalchemy import text

    # Get subscription
    result = await db.execute(text("SELECT user_id FROM subscriptions WHERE id = :sub_id"), {"sub_id": sub_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Activate subscription
    await db.execute(text("UPDATE subscriptions SET status = 'active' WHERE id = :sub_id"), {"sub_id": sub_id})

    # Grant verified badge
    await db.execute(text("UPDATE users SET is_verified = TRUE WHERE id = :user_id"), {"user_id": str(row[0])})

    return {"message": "Subscription approved and verified badge granted"}


@router.patch("/subscriptions/{sub_id}/revoke")
async def revoke_subscription(sub_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Revoke a subscription and remove verified badge."""
    from sqlalchemy import text

    result = await db.execute(text("SELECT user_id FROM subscriptions WHERE id = :sub_id"), {"sub_id": sub_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")

    await db.execute(text("UPDATE subscriptions SET status = 'cancelled' WHERE id = :sub_id"), {"sub_id": sub_id})
    await db.execute(text("UPDATE users SET is_verified = FALSE WHERE id = :user_id"), {"user_id": str(row[0])})

    return {"message": "Subscription revoked and verified badge removed"}


@router.patch("/users/{user_id}/verify")
async def toggle_user_verification(user_id: str, data: dict, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Manually verify/unverify a user (admin override)."""
    verified = data.get("is_verified", True)
    await db.execute(update(User).where(User.id == UUID(user_id)).values(is_verified=verified))
    db.add(AuditLog(admin_id=admin.id, action="verify_user" if verified else "unverify_user", target_type="user", target_id=UUID(user_id)))
    return {"message": f"User {'verified' if verified else 'unverified'}"}
