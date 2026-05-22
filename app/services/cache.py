"""Redis caching service."""

import json
from typing import Optional, Any
from redis import asyncio as aioredis
from app.config import get_settings

settings = get_settings()

redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Get or create Redis connection."""
    global redis_client
    if redis_client is None:
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return redis_client


class CacheService:
    """Redis caching utilities."""

    @staticmethod
    async def get(key: str) -> Optional[Any]:
        """Get cached value."""
        r = await get_redis()
        value = await r.get(key)
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return None

    @staticmethod
    async def set(key: str, value: Any, ttl: int = None):
        """Set cached value with optional TTL."""
        r = await get_redis()
        serialized = json.dumps(value) if not isinstance(value, str) else value
        if ttl:
            await r.setex(key, ttl, serialized)
        else:
            await r.setex(key, settings.REDIS_CACHE_TTL, serialized)

    @staticmethod
    async def delete(key: str):
        """Delete cached value."""
        r = await get_redis()
        await r.delete(key)

    @staticmethod
    async def delete_pattern(pattern: str):
        """Delete all keys matching pattern."""
        r = await get_redis()
        keys = []
        async for key in r.scan_iter(match=pattern):
            keys.append(key)
        if keys:
            await r.delete(*keys)

    @staticmethod
    async def increment(key: str, amount: int = 1) -> int:
        """Increment a counter."""
        r = await get_redis()
        return await r.incrby(key, amount)

    @staticmethod
    async def get_or_set(key: str, factory, ttl: int = None) -> Any:
        """Get from cache or compute and cache."""
        cached = await CacheService.get(key)
        if cached is not None:
            return cached
        value = await factory() if callable(factory) else factory
        await CacheService.set(key, value, ttl)
        return value


# Cache key builders
def user_cache_key(user_id: str) -> str:
    return f"user:{user_id}"


def profile_cache_key(user_id: str) -> str:
    return f"profile:{user_id}"


def feed_cache_key(user_id: str, page: int) -> str:
    return f"feed:{user_id}:page:{page}"


def trending_cache_key() -> str:
    return "trending:hashtags"


def notifications_count_key(user_id: str) -> str:
    return f"notifications:unread:{user_id}"


def online_users_key() -> str:
    return "online:users"
