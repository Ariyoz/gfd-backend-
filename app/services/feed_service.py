"""Feed service — personalized feed generation, trending, search."""

from uuid import UUID
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_

from app.models import Post, Follow, Like, Bookmark, Hashtag, PostVisibility
from app.services.cache import CacheService, feed_cache_key, trending_cache_key


class FeedService:
    """Business logic for feed generation."""

    @staticmethod
    async def get_personalized_feed(db: AsyncSession, user_id: UUID, page: int = 1, limit: int = 20) -> List[Post]:
        """Get feed with posts from followed users + trending."""
        # Check cache first
        cache_key = feed_cache_key(str(user_id), page)
        cached = await CacheService.get(cache_key)
        if cached:
            return cached

        offset = (page - 1) * limit

        # Get IDs of users this person follows
        following_result = await db.execute(
            select(Follow.following_id).where(Follow.follower_id == user_id)
        )
        following_ids = [row[0] for row in following_result.fetchall()]
        following_ids.append(user_id)  # Include own posts

        # Fetch posts from followed users
        result = await db.execute(
            select(Post)
            .where(
                and_(
                    Post.author_id.in_(following_ids),
                    Post.visibility == PostVisibility.PUBLIC,
                )
            )
            .order_by(desc(Post.created_at))
            .offset(offset)
            .limit(limit)
        )
        posts = result.scalars().all()

        # If not enough posts, fill with trending/recent public posts
        if len(posts) < limit:
            remaining = limit - len(posts)
            existing_ids = [p.id for p in posts]
            fill_result = await db.execute(
                select(Post)
                .where(
                    and_(
                        Post.visibility == PostVisibility.PUBLIC,
                        Post.id.notin_(existing_ids) if existing_ids else True,
                    )
                )
                .order_by(desc(Post.like_count), desc(Post.created_at))
                .limit(remaining)
            )
            posts.extend(fill_result.scalars().all())

        return posts

    @staticmethod
    async def get_explore_feed(db: AsyncSession, page: int = 1, limit: int = 20) -> List[Post]:
        """Get explore/discover feed — trending posts."""
        offset = (page - 1) * limit
        result = await db.execute(
            select(Post)
            .where(Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.like_count + Post.comment_count + Post.repost_count), desc(Post.created_at))
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()

    @staticmethod
    async def get_trending_hashtags(db: AsyncSession, limit: int = 20) -> List[dict]:
        """Get trending hashtags with caching."""
        cached = await CacheService.get(trending_cache_key())
        if cached:
            return cached

        result = await db.execute(
            select(Hashtag).order_by(desc(Hashtag.post_count)).limit(limit)
        )
        hashtags = [{"name": h.name, "count": h.post_count} for h in result.scalars().all()]

        await CacheService.set(trending_cache_key(), hashtags, ttl=300)  # 5 min cache
        return hashtags

    @staticmethod
    async def search_posts(db: AsyncSession, query: str, page: int = 1, limit: int = 20) -> List[Post]:
        """Search posts by content or hashtag."""
        offset = (page - 1) * limit

        if query.startswith("#"):
            # Hashtag search
            tag = query[1:].lower()
            result = await db.execute(
                select(Post)
                .where(Post.hashtags.any(tag))
                .order_by(desc(Post.created_at))
                .offset(offset)
                .limit(limit)
            )
        else:
            # Full-text search
            result = await db.execute(
                select(Post)
                .where(Post.content.ilike(f"%{query}%"))
                .order_by(desc(Post.created_at))
                .offset(offset)
                .limit(limit)
            )

        return result.scalars().all()
