"""Explore & Discovery endpoints — developer search, trending, recommendations."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List

from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user
from app.services.recommendation import RecommendationService

router = APIRouter()


@router.get("/developers")
async def explore_developers(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    skills: Optional[str] = Query(None, description="Comma-separated skills"),
    location: Optional[str] = Query(None),
    experience_level: Optional[str] = Query(None),
    available_only: bool = Query(False),
    min_rate: Optional[float] = Query(None),
    max_rate: Optional[float] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Explore developers — automatically shows all developers who signed up."""
    from sqlalchemy import select, desc, func, and_
    from app.models import User, DeveloperProfile, Follow, UserRole, UserStatus

    offset = (page - 1) * limit

    # Base query — ALL active developers
    query = (
        select(User, DeveloperProfile)
        .join(DeveloperProfile, DeveloperProfile.user_id == User.id)
        .where(
            and_(
                User.role == UserRole.DEVELOPER,
                User.status == UserStatus.ACTIVE,
            )
        )
    )

    # Apply filters
    if search:
        query = query.where(
            User.full_name.ilike(f"%{search}%") |
            User.username.ilike(f"%{search}%") |
            DeveloperProfile.bio.ilike(f"%{search}%")
        )
    if skills:
        skill_list = [s.strip() for s in skills.split(",")]
        query = query.where(DeveloperProfile.skills.overlap(skill_list))
    if location:
        query = query.where(DeveloperProfile.location.ilike(f"%{location}%"))
    if experience_level:
        query = query.where(DeveloperProfile.experience_level == experience_level)
    if available_only:
        query = query.where(DeveloperProfile.available_for_hire == True)
    if min_rate:
        query = query.where(DeveloperProfile.hourly_rate >= min_rate)
    if max_rate:
        query = query.where(DeveloperProfile.hourly_rate <= max_rate)

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Order by most recent, then apply pagination
    query = query.order_by(desc(User.created_at)).offset(offset).limit(limit)

    result = await db.execute(query)
    rows = result.all()

    developers = []
    for user, profile in rows:
        # Get follower count
        fc = (await db.execute(
            select(func.count()).where(Follow.following_id == user.id)
        )).scalar() or 0

        developers.append({
            "id": str(user.id),
            "username": user.username,
            "full_name": user.full_name,
            "avatar": user.avatar,
            "bio": profile.bio,
            "location": profile.location,
            "skills": profile.skills or [],
            "tech_stack": profile.tech_stack or [],
            "experience_level": profile.experience_level,
            "years_of_experience": profile.years_of_experience,
            "hourly_rate": profile.hourly_rate,
            "available_for_hire": profile.available_for_hire,
            "github_url": profile.github_url,
            "portfolio_url": profile.portfolio_url,
            "follower_count": fc,
            "is_verified": user.is_verified,
            "created_at": str(user.created_at),
        })

    return {"developers": developers, "page": page, "total": total, "has_more": len(developers) == limit}


@router.get("/search")
async def search_users(
    q: str = Query(..., min_length=1, description="Search query"),
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
    """Get trending hashtags, skills, and technologies."""
    from app.models import Hashtag
    from sqlalchemy import select, desc

    # Trending hashtags
    result = await db.execute(select(Hashtag).order_by(desc(Hashtag.post_count)).limit(20))
    hashtags = [{"name": h.name, "count": h.post_count} for h in result.scalars().all()]

    # Trending skills (from developer profiles)
    from app.models import DeveloperProfile
    from sqlalchemy import func
    # Get most common skills
    skills_result = await db.execute(
        select(func.unnest(DeveloperProfile.skills).label("skill"), func.count().label("count"))
        .group_by("skill")
        .order_by(desc("count"))
        .limit(20)
    )
    trending_skills = [{"skill": row[0], "count": row[1]} for row in skills_result.fetchall()]

    return {"hashtags": hashtags, "skills": trending_skills}
