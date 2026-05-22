"""User profile endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, DeveloperProfile, ClientProfile, Follow
from app.core.dependencies import get_current_active_user

router = APIRouter()


@router.get("/me")
async def get_me(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get current user profile."""
    return {"id": str(user.id), "email": user.email, "username": user.username, "full_name": user.full_name, "role": user.role.value, "avatar": user.avatar}


@router.patch("/me")
async def update_me(updates: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Update current user profile."""
    allowed = {"full_name", "avatar", "banner", "username"}
    for key, value in updates.items():
        if key in allowed:
            setattr(user, key, value)
    return {"message": "Profile updated"}


@router.get("/{user_id}")
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
    """Get public user profile by ID."""
    from uuid import UUID
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"id": str(user.id), "username": user.username, "full_name": user.full_name, "avatar": user.avatar, "role": user.role.value}


@router.post("/{user_id}/follow", status_code=status.HTTP_201_CREATED)
async def follow_user(user_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Follow a user."""
    from uuid import UUID
    target_id = UUID(user_id)
    if target_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")

    existing = await db.execute(select(Follow).where(Follow.follower_id == user.id, Follow.following_id == target_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already following")

    db.add(Follow(follower_id=user.id, following_id=target_id))
    return {"message": "Followed"}


@router.delete("/{user_id}/follow", status_code=status.HTTP_204_NO_CONTENT)
async def unfollow_user(user_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Unfollow a user."""
    from uuid import UUID
    target_id = UUID(user_id)
    result = await db.execute(select(Follow).where(Follow.follower_id == user.id, Follow.following_id == target_id))
    follow = result.scalar_one_or_none()
    if follow:
        await db.delete(follow)


@router.get("/{user_id}/followers")
async def get_followers(user_id: str, db: AsyncSession = Depends(get_db)):
    """Get user's followers."""
    from uuid import UUID
    result = await db.execute(select(Follow).where(Follow.following_id == UUID(user_id)))
    follows = result.scalars().all()
    return {"count": len(follows), "follower_ids": [str(f.follower_id) for f in follows]}


@router.get("/{user_id}/following")
async def get_following(user_id: str, db: AsyncSession = Depends(get_db)):
    """Get users this user follows."""
    from uuid import UUID
    result = await db.execute(select(Follow).where(Follow.follower_id == UUID(user_id)))
    follows = result.scalars().all()
    return {"count": len(follows), "following_ids": [str(f.following_id) for f in follows]}
