"""Cloudinary file upload integration."""

import cloudinary
import cloudinary.uploader
from fastapi import UploadFile, HTTPException
from app.config import get_settings

settings = get_settings()

cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_FILE_TYPES = {*ALLOWED_IMAGE_TYPES, "application/pdf"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


async def upload_image(file: UploadFile, folder: str = "gfd", transformation: dict = None) -> dict:
    """Upload an image to Cloudinary with optimization."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid image type. Allowed: {ALLOWED_IMAGE_TYPES}")

    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image too large. Max 5MB.")

    options = {
        "folder": folder,
        "resource_type": "image",
        "quality": "auto:good",
        "fetch_format": "auto",
    }
    if transformation:
        options["transformation"] = transformation

    result = cloudinary.uploader.upload(content, **options)
    return {
        "url": result["secure_url"],
        "public_id": result["public_id"],
        "width": result.get("width"),
        "height": result.get("height"),
        "format": result.get("format"),
        "size": result.get("bytes"),
    }


async def upload_file(file: UploadFile, folder: str = "gfd/files") -> dict:
    """Upload a file (PDF, resume, etc.) to Cloudinary."""
    if file.content_type not in ALLOWED_FILE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid file type. Allowed: {ALLOWED_FILE_TYPES}")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Max 10MB.")

    result = cloudinary.uploader.upload(
        content,
        folder=folder,
        resource_type="raw" if file.content_type == "application/pdf" else "image",
    )
    return {
        "url": result["secure_url"],
        "public_id": result["public_id"],
        "format": result.get("format"),
        "size": result.get("bytes"),
        "original_filename": file.filename,
    }


async def upload_avatar(file: UploadFile, user_id: str) -> str:
    """Upload and optimize avatar image."""
    result = await upload_image(
        file,
        folder="gfd/avatars",
        transformation={"width": 400, "height": 400, "crop": "fill", "gravity": "face"},
    )
    return result["url"]


async def upload_banner(file: UploadFile, user_id: str) -> str:
    """Upload and optimize banner image."""
    result = await upload_image(
        file,
        folder="gfd/banners",
        transformation={"width": 1500, "height": 500, "crop": "fill"},
    )
    return result["url"]


async def upload_post_media(file: UploadFile) -> dict:
    """Upload post media (image/video)."""
    if file.content_type and file.content_type.startswith("video/"):
        content = await file.read()
        if len(content) > 50 * 1024 * 1024:  # 50MB for video
            raise HTTPException(status_code=400, detail="Video too large. Max 50MB.")
        result = cloudinary.uploader.upload(content, folder="gfd/posts", resource_type="video")
        return {"url": result["secure_url"], "type": "video", "public_id": result["public_id"]}

    result = await upload_image(file, folder="gfd/posts")
    return {**result, "type": "image"}


def delete_file(public_id: str, resource_type: str = "image"):
    """Delete a file from Cloudinary."""
    cloudinary.uploader.destroy(public_id, resource_type=resource_type)
