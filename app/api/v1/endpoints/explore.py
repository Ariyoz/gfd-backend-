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
    db: AsyncSession = Depends(get_db),
):
    """Explore developers with smart filtering and ranking."""
    filters = {}
    if skills:
        filters["skills"] = [s.strip() for s in skills.split(",")]
    if location:
        filters["location"] = location
    if experience_level:
        filters["experience_level"] = experience_level
    if available_only:
        filters["available_only"] = True
    if min_rate:
        filters["min_rate"] = min_rate
    if max_rate:
        filters["max_rate"] = max_rate

    developers = await RecommendationService.get_trending_developers(db, limit=limit, filters=filters or None)
    return {"developers": developers, "page": page, "total": len(developers)}


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
