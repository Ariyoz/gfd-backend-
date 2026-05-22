"""Feed & social endpoints — posts, likes, comments, bookmarks."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from uuid import UUID
from typing import Optional

from app.database import get_db
from app.models import Post, Comment, Like, Bookmark, User, PostType, PostVisibility
from app.core.dependencies import get_current_active_user

router = APIRouter()


@router.get("/")
async def get_feed(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get personalized feed."""
    offset = (page - 1) * limit
    result = await db.execute(
        select(Post)
        .where(Post.visibility == PostVisibility.PUBLIC)
        .order_by(desc(Post.created_at))
        .offset(offset)
        .limit(limit)
    )
    posts = result.scalars().all()
    return {"posts": posts, "page": page, "limit": limit}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_post(data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Create a new post."""
    post = Post(
        author_id=user.id,
        content=data.get("content"),
        post_type=PostType(data.get("post_type", "text")),
        visibility=PostVisibility(data.get("visibility", "public")),
        media_urls=data.get("media_urls", []),
        code_snippet=data.get("code_snippet"),
        code_language=data.get("code_language"),
        hashtags=data.get("hashtags", []),
    )
    db.add(post)
    await db.flush()
    return {"id": str(post.id), "message": "Post created"}


@router.get("/{post_id}")
async def get_post(post_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single post."""
    result = await db.execute(select(Post).where(Post.id == UUID(post_id)))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Delete own post."""
    result = await db.execute(select(Post).where(Post.id == UUID(post_id), Post.author_id == user.id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    await db.delete(post)


@router.post("/{post_id}/like", status_code=status.HTTP_201_CREATED)
async def like_post(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Like a post."""
    pid = UUID(post_id)
    existing = await db.execute(select(Like).where(Like.user_id == user.id, Like.post_id == pid))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already liked")
    db.add(Like(user_id=user.id, post_id=pid))
    await db.execute(select(Post).where(Post.id == pid))  # Increment handled by trigger or app logic
    return {"message": "Liked"}


@router.delete("/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT)
async def unlike_post(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Unlike a post."""
    pid = UUID(post_id)
    result = await db.execute(select(Like).where(Like.user_id == user.id, Like.post_id == pid))
    like = result.scalar_one_or_none()
    if like:
        await db.delete(like)


@router.post("/{post_id}/comment", status_code=status.HTTP_201_CREATED)
async def add_comment(post_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Add a comment to a post."""
    comment = Comment(
        post_id=UUID(post_id),
        author_id=user.id,
        content=data["content"],
        parent_comment_id=UUID(data["parent_id"]) if data.get("parent_id") else None,
    )
    db.add(comment)
    await db.flush()
    return {"id": str(comment.id), "message": "Comment added"}


@router.post("/{post_id}/bookmark", status_code=status.HTTP_201_CREATED)
async def bookmark_post(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Bookmark a post."""
    pid = UUID(post_id)
    existing = await db.execute(select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == pid))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already bookmarked")
    db.add(Bookmark(user_id=user.id, post_id=pid))
    return {"message": "Bookmarked"}


@router.get("/trending/hashtags")
async def trending_hashtags(db: AsyncSession = Depends(get_db)):
    """Get trending hashtags."""
    from app.models import Hashtag
    result = await db.execute(select(Hashtag).order_by(desc(Hashtag.post_count)).limit(20))
    return {"hashtags": result.scalars().all()}
