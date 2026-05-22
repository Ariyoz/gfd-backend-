"""Smart recommendation engine for users, posts, and developers."""

from uuid import UUID
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_, or_, not_

from app.models import (
    User, DeveloperProfile, Follow, Post, Like, GitHubProfile,
    UserRole, UserStatus,
)
from app.services.cache import CacheService


class RecommendationService:
    """Recommendation engine for the platform."""

    @staticmethod
    async def get_suggested_users(db: AsyncSession, user_id: UUID, limit: int = 10) -> List[dict]:
        """Get suggested users to follow based on mutual interests and followers."""
        cache_key = f"recommendations:users:{user_id}:{limit}"
        cached = await CacheService.get(cache_key)
        if cached:
            return cached

        # Get current user's following list
        following_result = await db.execute(
            select(Follow.following_id).where(Follow.follower_id == user_id)
        )
        following_ids = [row[0] for row in following_result.fetchall()]
        following_ids.append(user_id)  # Exclude self

        # Find users followed by people you follow (mutual interest)
        if following_ids:
            mutual_result = await db.execute(
                select(Follow.following_id, func.count().label("mutual_count"))
                .where(
                    and_(
                        Follow.follower_id.in_(following_ids),
                        Follow.following_id.notin_(following_ids),
                    )
                )
                .group_by(Follow.following_id)
                .order_by(desc("mutual_count"))
                .limit(limit)
            )
            suggested_ids = [row[0] for row in mutual_result.fetchall()]
        else:
            suggested_ids = []

        # If not enough suggestions, fill with popular users
        if len(suggested_ids) < limit:
            remaining = limit - len(suggested_ids)
            exclude_ids = following_ids + suggested_ids
            popular_result = await db.execute(
                select(User.id)
                .where(
                    and_(
                        User.id.notin_(exclude_ids),
                        User.status == UserStatus.ACTIVE,
                        User.role == UserRole.DEVELOPER,
                    )
                )
                .limit(remaining)
            )
            suggested_ids.extend([row[0] for row in popular_result.fetchall()])

        # Fetch user details
        if not suggested_ids:
            return []

        users_result = await db.execute(
            select(User).where(User.id.in_(suggested_ids))
        )
        users = users_result.scalars().all()

        suggestions = [{
            "id": str(u.id),
            "username": u.username,
            "full_name": u.full_name,
            "avatar": u.avatar,
            "role": u.role.value,
        } for u in users]

        await CacheService.set(cache_key, suggestions, ttl=600)  # 10 min cache
        return suggestions

    @staticmethod
    async def get_trending_developers(db: AsyncSession, limit: int = 20, filters: dict = None) -> List[dict]:
        """Get trending developers for the Explore page."""
        cache_key = f"explore:trending:{limit}:{hash(str(filters))}"
        cached = await CacheService.get(cache_key)
        if cached:
            return cached

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
        if filters:
            if filters.get("skills"):
                query = query.where(DeveloperProfile.skills.overlap(filters["skills"]))
            if filters.get("location"):
                query = query.where(DeveloperProfile.location.ilike(f"%{filters['location']}%"))
            if filters.get("experience_level"):
                query = query.where(DeveloperProfile.experience_level == filters["experience_level"])
            if filters.get("available_only"):
                query = query.where(DeveloperProfile.available_for_hire == True)
            if filters.get("min_rate"):
                query = query.where(DeveloperProfile.hourly_rate >= filters["min_rate"])
            if filters.get("max_rate"):
                query = query.where(DeveloperProfile.hourly_rate <= filters["max_rate"])

        # Order by engagement (follower count as proxy)
        query = query.limit(limit)

        result = await db.execute(query)
        rows = result.all()

        developers = []
        for user, profile in rows:
            # Get follower count
            fc = await db.execute(
                select(func.count()).where(Follow.following_id == user.id)
            )
            follower_count = fc.scalar() or 0

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
                "hourly_rate": profile.hourly_rate,
                "available_for_hire": profile.available_for_hire,
                "github_url": profile.github_url,
                "portfolio_url": profile.portfolio_url,
                "follower_count": follower_count,
            })

        # Sort by follower count
        developers.sort(key=lambda d: d["follower_count"], reverse=True)

        await CacheService.set(cache_key, developers, ttl=300)  # 5 min cache
        return developers

    @staticmethod
    async def get_suggested_posts(db: AsyncSession, user_id: UUID, limit: int = 10) -> List[dict]:
        """Get suggested posts based on user interests."""
        # Get user's liked post hashtags
        liked_result = await db.execute(
            select(Post.hashtags)
            .join(Like, Like.post_id == Post.id)
            .where(Like.user_id == user_id)
            .limit(50)
        )
        user_hashtags = set()
        for row in liked_result.fetchall():
            if row[0]:
                user_hashtags.update(row[0])

        # Find posts with similar hashtags that user hasn't seen
        if user_hashtags:
            query = (
                select(Post)
                .where(
                    and_(
                        Post.author_id != user_id,
                        Post.hashtags.overlap(list(user_hashtags)),
                    )
                )
                .order_by(desc(Post.like_count), desc(Post.created_at))
                .limit(limit)
            )
        else:
            # Fallback to popular posts
            query = (
                select(Post)
                .where(Post.author_id != user_id)
                .order_by(desc(Post.like_count), desc(Post.created_at))
                .limit(limit)
            )

        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def search_users(db: AsyncSession, query: str, filters: dict = None, limit: int = 20) -> List[dict]:
        """Search users by name, username, skills, location."""
        search_query = (
            select(User, DeveloperProfile)
            .outerjoin(DeveloperProfile, DeveloperProfile.user_id == User.id)
            .where(
                and_(
                    User.status == UserStatus.ACTIVE,
                    or_(
                        User.username.ilike(f"%{query}%"),
                        User.full_name.ilike(f"%{query}%"),
                        DeveloperProfile.skills.any(query),
                        DeveloperProfile.location.ilike(f"%{query}%"),
                    )
                )
            )
            .limit(limit)
        )

        result = await db.execute(search_query)
        rows = result.all()

        return [{
            "id": str(user.id),
            "username": user.username,
            "full_name": user.full_name,
            "avatar": user.avatar,
            "role": user.role.value,
            "bio": profile.bio if profile else None,
            "skills": profile.skills if profile else [],
            "location": profile.location if profile else None,
        } for user, profile in rows]
