"""File upload endpoints."""

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from app.models import User
from app.core.dependencies import get_current_active_user

router = APIRouter()


@router.post("/avatar")
async def upload_user_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
):
    """Upload user avatar."""
    try:
        from app.integrations.cloudinary_service import upload_avatar
        url = await upload_avatar(file, str(user.id))
        return {"url": url}
    except ImportError:
        raise HTTPException(status_code=503, detail="Cloudinary not configured")


@router.post("/banner")
async def upload_user_banner(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
):
    """Upload user banner."""
    try:
        from app.integrations.cloudinary_service import upload_banner
        url = await upload_banner(file, str(user.id))
        return {"url": url}
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
