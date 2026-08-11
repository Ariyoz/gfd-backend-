"""Explore & Discovery endpoints — developer search, trending, recommendations."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_, text
from typing import Optional

from app.database import get_db
from app.models import User, DeveloperProfile, Follow, UserRole, UserStatus
from app.core.dependencies import get_current_active_user
from app.services.recommendation import RecommendationService

router = APIRouter()


@router.get("/developers")
async def explore_developers(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    skills: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    experience_level: Optional[str] = Query(None),
    available_only: bool = Query(False),
    min_rate: Optional[float] = Query(None),
    max_rate: Optional[float] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Explore developers with a SINGLE optimised query.
    Follower counts fetched via subquery — no N+1.
    """
    offset = (page - 1) * limit

    # ── Build WHERE conditions ────────────────────────────────────────────────
    conditions = [
        User.role   == UserRole.DEVELOPER,
        User.status == UserStatus.ACTIVE,
    ]
    if search:
        like = f"%{search}%"
        conditions.append(
            User.full_name.ilike(like) |
            User.username.ilike(like) |
            DeveloperProfile.bio.ilike(like)
        )
    if skills:
        skill_list = [s.strip() for s in skills.split(",") if s.strip()]
        if skill_list:
            conditions.append(DeveloperProfile.skills.overlap(skill_list))
    if location:
        conditions.append(DeveloperProfile.location.ilike(f"%{location}%"))
    if experience_level:
        conditions.append(DeveloperProfile.experience_level == experience_level)
    if available_only:
        conditions.append(DeveloperProfile.available_for_hire == True)
    if min_rate is not None:
        conditions.append(DeveloperProfile.hourly_rate >= min_rate)
    if max_rate is not None:
        conditions.append(DeveloperProfile.hourly_rate <= max_rate)

    # ── Follower count subquery (no N+1) ─────────────────────────────────────
    follower_sq = (
        select(Follow.following_id, func.count().label("fc"))
        .group_by(Follow.following_id)
        .subquery()
    )

    # ── Main query: single join ───────────────────────────────────────────────
    base_q = (
        select(
            User, DeveloperProfile,
            func.coalesce(follower_sq.c.fc, 0).label("follower_count"),
        )
        .join(DeveloperProfile, DeveloperProfile.user_id == User.id)
        .outerjoin(follower_sq, follower_sq.c.following_id == User.id)
        .where(and_(*conditions))
    )

    # ── Count (reuse same conditions) ────────────────────────────────────────
    count_q = (
        select(func.count())
        .select_from(User)
        .join(DeveloperProfile, DeveloperProfile.user_id == User.id)
        .where(and_(*conditions))
    )

    total, rows = await _run_parallel(db, count_q, base_q, offset, limit)

    developers = [
        {
            "id":                  str(u.id),
            "username":            u.username,
            "full_name":           u.full_name,
            "avatar":              u.avatar,
            "banner":              u.banner,
            "bio":                 p.bio,
            "location":            p.location,
            "skills":              p.skills or [],
            "tech_stack":          p.tech_stack or [],
            "experience_level":    p.experience_level,
            "years_of_experience": p.years_of_experience,
            "hourly_rate":         p.hourly_rate,
            "available_for_hire":  p.available_for_hire,
            "github_url":          p.github_url,
            "portfolio_url":       p.portfolio_url,
            "follower_count":      int(fc),
            "is_verified":         u.is_verified,
            "created_at":          str(u.created_at),
        }
        for u, p, fc in rows
    ]

    return {
        "developers": developers,
        "page":       page,
        "total":      total,
        "has_more":   len(developers) == limit,
    }


async def _run_parallel(db, count_q, data_q, offset, limit):
    """Run count and data queries, return (total, rows)."""
    total_result = await db.execute(count_q)
    total = total_result.scalar() or 0

    data_result = await db.execute(
        data_q.order_by(desc(User.created_at)).offset(offset).limit(limit)
    )
    return total, data_result.all()


@router.get("/search")
async def search_users(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Search users by name, username, skills, location."""
    results = await RecommendationService.search_users(db, query=q, limit=limit)
    return {"results": results, "query": q, "total": len(results)}


@router.get("/suggestions")
async def get_suggestions(
    limit: int = Query(10, ge=1, le=50),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get personalized user suggestions (who to follow)."""
    suggestions = await RecommendationService.get_suggested_users(db, user.id, limit=limit)
    return {"suggestions": suggestions}


@router.get("/trending")
async def get_trending(db: AsyncSession = Depends(get_db)):
    """Get trending hashtags and skills — cached 10 min."""
    from app.services.cache import CacheService

    CACHE_KEY = "trending:v2"
    cached = await CacheService.get(CACHE_KEY)
    if cached:
        return cached

    try:
        from app.models import Hashtag
        hashtags_result = await db.execute(
            select(Hashtag).order_by(desc(Hashtag.post_count)).limit(20)
        )
        hashtags = [{"name": h.name, "count": h.post_count}
                    for h in hashtags_result.scalars().all()]
    except Exception:
        hashtags = []

    try:
        skills_result = await db.execute(
            select(func.unnest(DeveloperProfile.skills).label("skill"),
                   func.count().label("count"))
            .group_by(text("skill"))
            .order_by(desc("count"))
            .limit(20)
        )
        trending_skills = [{"skill": r[0], "count": r[1]}
                           for r in skills_result.fetchall()]
    except Exception:
        trending_skills = []

    result = {"hashtags": hashtags, "skills": trending_skills}
    await CacheService.set(CACHE_KEY, result, ttl=600)
    return result


@router.get("/stats")
async def get_platform_stats(db: AsyncSession = Depends(get_db)):
    """Platform stats — single optimised query, cached 5 min."""
    from app.services.cache import CacheService

    CACHE_KEY = "platform:stats:v2"
    try:
        cached = await CacheService.get(CACHE_KEY)
        if cached:
            return cached
    except Exception:
        pass  # Redis may be down — continue without cache

    try:
        # Use CAST to text to handle both enum and varchar status columns
        row = await db.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE role::text = 'developer' AND status::text IN ('active','ACTIVE')) AS developers,
                COUNT(*) FILTER (WHERE role::text = 'client'    AND status::text IN ('active','ACTIVE')) AS clients,
                COUNT(*) FILTER (WHERE status::text IN ('active','ACTIVE','pending_verification','PENDING_VERIFICATION')) AS total_users
            FROM users
        """))
        stats = row.mappings().first()

        project_count = 0
        try:
            from app.models import Project
            project_count = (await db.execute(
                select(func.count()).select_from(Project)
            )).scalar() or 0
        except Exception:
            pass

        result = {
            "developers":         int(stats["developers"] or 0),
            "companies_hiring":   int(stats["clients"] or 0),
            "projects_delivered": project_count,
            "total_users":        int(stats["total_users"] or 0),
        }

        try:
            await CacheService.set(CACHE_KEY, result, ttl=300)
        except Exception:
            pass

        return result
    except Exception as e:
        import traceback; traceback.print_exc()
        # Fallback — count all users without filter
        try:
            row = await db.execute(text("SELECT COUNT(*) AS total FROM users"))
            total = int(row.scalar() or 0)
            return {
                "developers":         max(total - 2, 0),
                "companies_hiring":   2,
                "projects_delivered": 0,
                "total_users":        total,
            }
        except Exception:
            return {"developers": 0, "companies_hiring": 0, "projects_delivered": 0, "total_users": 0}
