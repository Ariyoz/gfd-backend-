"""Cloudinary file upload integration — upgraded for Phase 2."""

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
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime", "video/mpeg"}
ALLOWED_ATTACHMENT_TYPES = {
    *ALLOWED_IMAGE_TYPES,
    "application/pdf",
    "application/zip",
    "application/x-zip-compressed",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
    "application/octet-stream",
}

MAX_IMAGE_SIZE = 10 * 1024 * 1024   # 10 MB
MAX_FILE_SIZE  = 20 * 1024 * 1024   # 20 MB (message attachments & documents)
MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100 MB


async def upload_image(file: UploadFile, folder: str = "gfd", transformation: dict = None) -> dict:
    """Upload an image to Cloudinary with auto-optimization."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid image type. Allowed: jpeg, png, gif, webp")

    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image too large. Max 10 MB.")

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
    """Upload a file (PDF, archive, etc.) to Cloudinary. Max 20 MB."""
    if file.content_type not in ALLOWED_ATTACHMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Allowed: images, PDF, ZIP, RAR, 7Z"
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Max 20 MB.")

    is_image = file.content_type in ALLOWED_IMAGE_TYPES
    result = cloudinary.uploader.upload(
        content,
        folder=folder,
        resource_type="image" if is_image else "raw",
    )
    return {
        "url": result["secure_url"],
        "public_id": result["public_id"],
        "format": result.get("format"),
        "size": result.get("bytes"),
        "original_filename": file.filename,
        "is_image": is_image,
    }


async def upload_avatar(file: UploadFile, user_id: str) -> str:
    """Upload and optimize avatar image (400x400 face crop)."""
    result = await upload_image(
        file,
        folder="gfd/avatars",
        transformation={"width": 400, "height": 400, "crop": "fill", "gravity": "face"},
    )
    return result["url"]


async def upload_banner(file: UploadFile, user_id: str) -> str:
    """Upload and optimize banner image (1500x500)."""
    result = await upload_image(
        file,
        folder="gfd/banners",
        transformation={"width": 1500, "height": 500, "crop": "fill"},
    )
    return result["url"]


async def upload_post_media(file: UploadFile) -> dict:
    """Upload post media — image (10 MB) or video (100 MB)."""
    content_type = file.content_type or ""

    if content_type.startswith("video/") or content_type in ALLOWED_VIDEO_TYPES:
        content = await file.read()
        if len(content) > MAX_VIDEO_SIZE:
            raise HTTPException(status_code=400, detail="Video too large. Max 100 MB.")
        result = cloudinary.uploader.upload(
            content,
            folder="gfd/posts",
            resource_type="video",
        )
        return {
            "url": result["secure_url"],
            "type": "video",
            "public_id": result["public_id"],
            "size": result.get("bytes"),
        }

    result = await upload_image(file, folder="gfd/posts")
    return {**result, "type": "image"}


def delete_file(public_id: str, resource_type: str = "image"):
    """Delete a file from Cloudinary."""
    cloudinary.uploader.destroy(public_id, resource_type=resource_type)
