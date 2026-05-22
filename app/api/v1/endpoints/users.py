"""User profile endpoints with real-time follow system."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID

from app.database import get_db
from app.models import User, DeveloperProfile, ClientProfile, Follow, Post, BlockedUser
from app.core.dependencies import get_current_active_user
from app.services.realtime import RealtimeService
from app.services.notification_service import NotificationService

router = APIRouter()


@router.get("/me")
async def get_me(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get current user full profile."""
    # Get follower/following counts
    followers = (await db.execute(select(func.count()).where(Follow.following_id == user.id))).scalar() or 0
    following = (await db.execute(select(func.count()).where(Follow.follower_id == user.id))).scalar() or 0
    post_count = (await db.execute(select(func.count()).where(Post.author_id == user.id))).scalar() or 0

    profile_data = {
        "id": str(user.id),
        "email": user.email,
        "username": user.username,
        "full_name": user.full_name,
        "avatar": user.avatar,
        "banner": user.banner,
        "role": user.role.value,
        "is_verified": user.is_verified,
        "is_online": user.is_online,
        "follower_count": followers,
        "following_count": following,
        "post_count": post_count,
    }

    # Add developer profile if exists
    dev_result = await db.execute(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
    dev_profile = dev_result.scalar_one_or_none()
    if dev_profile:
        profile_data.update({
            "bio": dev_profile.bio,
            "location": dev_profile.location,
            "skills": dev_profile.skills or [],
            "tech_stack": dev_profile.tech_stack or [],
            "experience_level": dev_profile.experience_level,
            "years_of_experience": dev_profile.years_of_experience,
            "hourly_rate": dev_profile.hourly_rate,
            "available_for_hire": dev_profile.available_for_hire,
            "github_url": dev_profile.github_url,
            "linkedin_url": dev_profile.linkedin_url,
            "portfolio_url": dev_profile.portfolio_url,
            "website_url": dev_profile.website_url,
        })

    # Add client profile if exists
    client_result = await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id))
    client_profile = client_result.scalar_one_or_none()
    if client_profile:
        profile_data.update({
            "company_name": client_profile.company_name,
            "company_bio": client_profile.company_bio,
            "website": client_profile.website,
            "location": client_profile.location,
            "industry": client_profile.industry,
        })

    return profile_data


@router.patch("/me")
async def update_me(updates: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Update current user profile."""
    # User table fields
    user_fields = {"full_name", "avatar", "banner", "username"}
    for key, value in updates.items():
        if key in user_fields:
            setattr(user, key, value)

    # Developer profile fields
    dev_fields = {"bio", "location", "skills", "tech_stack", "experience_level",
                  "years_of_experience", "hourly_rate", "available_for_hire",
                  "github_url", "linkedin_url", "portfolio_url", "website_url", "preferred_roles"}
    dev_updates = {k: v for k, v in updates.items() if k in dev_fields}

    if dev_updates:
        dev_result = await db.execute(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
        dev_profile = dev_result.scalar_one_or_none()
        if dev_profile:
            for key, value in dev_updates.items():
                setattr(dev_profile, key, value)

    # Client profile fields
    client_fields = {"company_name", "company_bio", "website", "industry"}
    client_updates = {k: v for k, v in updates.items() if k in client_fields}

    if client_updates:
        client_result = await db.execute(select(ClientProfile).where(ClientProfile.user_id == user.id))
        client_profile = client_result.scalar_one_or_none()
        if client_profile:
            for key, value in client_updates.items():
                setattr(client_profile, key, value)

    # Broadcast profile update
    await RealtimeService.on_profile_updated(user.id, updates)

    return {"message": "Profile updated"}


@router.get("/{user_id}")
async def get_user(user_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Get public user profile by ID."""
    uid = UUID(user_id)
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Counts
    followers = (await db.execute(select(func.count()).where(Follow.following_id == uid))).scalar() or 0
    following = (await db.execute(select(func.count()).where(Follow.follower_id == uid))).scalar() or 0
    post_count = (await db.execute(select(func.count()).where(Post.author_id == uid))).scalar() or 0

    # Check if current user follows this user
    is_following = False
    if current_user:
        follow_check = await db.execute(select(Follow).where(Follow.follower_id == current_user.id, Follow.following_id == uid))
        is_following = follow_check.scalar_one_or_none() is not None

    profile_data = {
        "id": str(user.id),
        "username": user.username,
        "full_name": user.full_name,
        "avatar": user.avatar,
        "banner": user.banner,
        "role": user.role.value,
        "is_verified": user.is_verified,
        "is_online": user.is_online,
        "follower_count": followers,
        "following_count": following,
        "post_count": post_count,
        "is_following": is_following,
    }

    # Developer profile
    dev_result = await db.execute(select(DeveloperProfile).where(DeveloperProfile.user_id == uid))
    dev_profile = dev_result.scalar_one_or_none()
    if dev_profile:
        profile_data.update({
            "bio": dev_profile.bio,
            "location": dev_profile.location,
            "skills": dev_profile.skills or [],
            "tech_stack": dev_profile.tech_stack or [],
            "experience_level": dev_profile.experience_level,
            "hourly_rate": dev_profile.hourly_rate,
            "available_for_hire": dev_profile.available_for_hire,
            "github_url": dev_profile.github_url,
            "portfolio_url": dev_profile.portfolio_url,
        })

    return profile_data


@router.post("/{user_id}/follow", status_code=status.HTTP_201_CREATED)
async def follow_user(user_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Follow a user — real-time notification."""
    target_id = UUID(user_id)
    if target_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")

    # Check if blocked
    blocked = await db.execute(select(BlockedUser).where(BlockedUser.blocker_id == target_id, BlockedUser.blocked_id == user.id))
    if blocked.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Cannot follow this user")

    existing = await db.execute(select(Follow).where(Follow.follower_id == user.id, Follow.following_id == target_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already following")

    db.add(Follow(follower_id=user.id, following_id=target_id))
    await db.flush()

    # Real-time broadcast
    await RealtimeService.on_user_followed(db, user, target_id)

    # Notification
    await NotificationService.notify_follow(db, target_id, user.id, user.full_name)

    return {"message": "Followed"}


@router.delete("/{user_id}/follow", status_code=status.HTTP_204_NO_CONTENT)
async def unfollow_user(user_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Unfollow a user."""
    target_id = UUID(user_id)
    result = await db.execute(select(Follow).where(Follow.follower_id == user.id, Follow.following_id == target_id))
    follow = result.scalar_one_or_none()
    if follow:
        await db.delete(follow)
        await RealtimeService.on_user_unfollowed(db, user.id, target_id)


@router.delete("/{user_id}/follower", status_code=status.HTTP_204_NO_CONTENT)
async def remove_follower(user_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Remove a follower."""
    follower_id = UUID(user_id)
    result = await db.execute(select(Follow).where(Follow.follower_id == follower_id, Follow.following_id == user.id))
    follow = result.scalar_one_or_none()
    if follow:
        await db.delete(follow)


@router.post("/{user_id}/block", status_code=status.HTTP_201_CREATED)
async def block_user(user_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Block a user."""
    target_id = UUID(user_id)
    if target_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot block yourself")

    existing = await db.execute(select(BlockedUser).where(BlockedUser.blocker_id == user.id, BlockedUser.blocked_id == target_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already blocked")

    db.add(BlockedUser(blocker_id=user.id, blocked_id=target_id))

    # Also unfollow both ways
    await db.execute(select(Follow).where(Follow.follower_id == user.id, Follow.following_id == target_id))
    await db.execute(select(Follow).where(Follow.follower_id == target_id, Follow.following_id == user.id))

    return {"message": "User blocked"}


@router.delete("/{user_id}/block", status_code=status.HTTP_204_NO_CONTENT)
async def unblock_user(user_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Unblock a user."""
    target_id = UUID(user_id)
    result = await db.execute(select(BlockedUser).where(BlockedUser.blocker_id == user.id, BlockedUser.blocked_id == target_id))
    block = result.scalar_one_or_none()
    if block:
        await db.delete(block)


@router.get("/{user_id}/followers")
async def get_followers(user_id: str, page: int = Query(1), limit: int = Query(20), db: AsyncSession = Depends(get_db)):
    """Get user's followers with profiles."""
    uid = UUID(user_id)
    offset = (page - 1) * limit
    result = await db.execute(
        select(User)
        .join(Follow, Follow.follower_id == User.id)
        .where(Follow.following_id == uid)
        .offset(offset)
        .limit(limit)
    )
    users = result.scalars().all()
    return {
        "followers": [{
            "id": str(u.id), "username": u.username, "full_name": u.full_name, "avatar": u.avatar, "role": u.role.value,
        } for u in users],
        "total": len(users),
    }


@router.get("/{user_id}/following")
async def get_following(user_id: str, page: int = Query(1), limit: int = Query(20), db: AsyncSession = Depends(get_db)):
    """Get users this user follows."""
    uid = UUID(user_id)
    offset = (page - 1) * limit
    result = await db.execute(
        select(User)
        .join(Follow, Follow.following_id == User.id)
        .where(Follow.follower_id == uid)
        .offset(offset)
        .limit(limit)
    )
    users = result.scalars().all()
    return {
        "following": [{
            "id": str(u.id), "username": u.username, "full_name": u.full_name, "avatar": u.avatar, "role": u.role.value,
        } for u in users],
        "total": len(users),
    }
