"""File upload endpoints."""

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user

router = APIRouter()


@router.post("/avatar")
async def upload_user_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload user avatar — updates globally via WebSocket broadcast."""
    try:
        from app.integrations.cloudinary_service import upload_avatar
        from app.database import get_db as get_db_dep
        from sqlalchemy import update
        from app.models import User as UserModel
        from app.websocket.events import broadcast_event, EventType

        url = await upload_avatar(file, str(user.id))

        # Update user avatar in database
        await db.execute(
            update(UserModel).where(UserModel.id == user.id).values(avatar=url)
        )
        await db.flush()

        # Broadcast avatar change to all connected users (so feeds update instantly)
        await broadcast_event(
            EventType.PROFILE_UPDATED,
            {"user_id": str(user.id), "avatar": url, "field": "avatar"},
        )

        return {"url": url, "message": "Avatar updated"}
    except ImportError:
        raise HTTPException(status_code=503, detail="Cloudinary not configured")


@router.post("/banner")
async def upload_user_banner(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload user banner — updates globally."""
    try:
        from app.integrations.cloudinary_service import upload_banner
        from sqlalchemy import update
        from app.models import User as UserModel
        from app.websocket.events import broadcast_event, EventType

        url = await upload_banner(file, str(user.id))

        await db.execute(
            update(UserModel).where(UserModel.id == user.id).values(banner=url)
        )
        await db.flush()

        await broadcast_event(
            EventType.PROFILE_UPDATED,
            {"user_id": str(user.id), "banner": url, "field": "banner"},
        )

        return {"url": url, "message": "Banner updated"}
    except ImportError:
        raise HTTPException(status_code=503, detail="Cloudinary not configured")


@router.post("/media")
async def upload_media(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
):
    """Upload post media (image/video)."""
    try:
        from app.integrations.cloudinary_service import upload_post_media
        result = await upload_post_media(file)
        return result
    except ImportError:
        raise HTTPException(status_code=503, detail="Cloudinary not configured")


@router.post("/file")
async def upload_document(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
):
    """Upload document (PDF, resume, etc.)."""
    try:
        from app.integrations.cloudinary_service import upload_file
        result = await upload_file(file, folder=f"gfd/users/{user.id}/files")
        return result
    except ImportError:
        raise HTTPException(status_code=503, detail="Cloudinary not configured")
