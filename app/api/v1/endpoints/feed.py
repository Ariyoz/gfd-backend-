"""Feed & social endpoints — optimised, no N+1 queries."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, update, and_, text
from uuid import UUID
from typing import Optional

from app.database import get_db
from app.models import (
    Post, Comment, Like, Bookmark, User,
    PostType, PostVisibility, Follow,
)
from app.core.dependencies import get_current_active_user
from app.services.realtime import RealtimeService
from app.services.notification_service import NotificationService

router = APIRouter()


# ── Shared enrichment helper ──────────────────────────────────────────────────

async def _enrich_posts(db: AsyncSession, posts: list[Post], viewer_id: UUID) -> list[dict]:
    """
    Enrich a list of Post ORM objects with author info + viewer's like/bookmark status.
    Uses IN queries — O(3) queries regardless of post count, not O(3n).
    """
    if not posts:
        return []

    post_ids    = [p.id for p in posts]
    author_ids  = list({p.author_id for p in posts})

    # 1. Fetch all authors in one query
    authors_result = await db.execute(
        select(User).where(User.id.in_(author_ids))
    )
    author_map = {u.id: u for u in authors_result.scalars().all()}

    # 2. Liked post ids for this viewer
    liked_result = await db.execute(
        select(Like.post_id).where(
            Like.user_id == viewer_id,
            Like.post_id.in_(post_ids),
        )
    )
    liked_ids = {row[0] for row in liked_result.fetchall()}

    # 3. Bookmarked post ids for this viewer
    bm_result = await db.execute(
        select(Bookmark.post_id).where(
            Bookmark.user_id == viewer_id,
            Bookmark.post_id.in_(post_ids),
        )
    )
    bookmarked_ids = {row[0] for row in bm_result.fetchall()}

    enriched = []
    for post in posts:
        author = author_map.get(post.author_id)
        enriched.append({
            "id":            str(post.id),
            "content":       post.content,
            "post_type":     post.post_type.value if post.post_type else "text",
            "media_urls":    post.media_urls or [],
            "code_snippet":  post.code_snippet,
            "code_language": post.code_language,
            "hashtags":      post.hashtags or [],
            "like_count":    post.like_count,
            "comment_count": post.comment_count,
            "repost_count":  post.repost_count,
            "bookmark_count":post.bookmark_count,
            "is_liked":      post.id in liked_ids,
            "is_bookmarked": post.id in bookmarked_ids,
            "is_edited":     post.is_edited,
            "created_at":    str(post.created_at),
            "author": {
                "id":          str(author.id)        if author else None,
                "username":    author.username       if author else None,
                "full_name":   author.full_name      if author else None,
                "avatar":      author.avatar         if author else None,
                "is_verified": author.is_verified    if author else False,
            } if author else None,
        })
    return enriched


# ── Feed ──────────────────────────────────────────────────────────────────────

@router.get("/")
async def get_feed(
    page:      int           = Query(1, ge=1),
    limit:     int           = Query(20, ge=1, le=100),
    feed_type: str           = Query("explore"),
    user_id:   Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get feed — following, explore, or user-specific. Zero N+1 queries."""
    offset = (page - 1) * limit

    if feed_type == "following":
        following_result = await db.execute(
            select(Follow.following_id).where(Follow.follower_id == user.id)
        )
        ids = [r[0] for r in following_result.fetchall()] + [user.id]
        q = (
            select(Post)
            .where(Post.author_id.in_(ids), Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.created_at))
            .offset(offset).limit(limit)
        )
    elif feed_type == "user" and user_id:
        q = (
            select(Post)
            .where(Post.author_id == UUID(user_id), Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.created_at))
            .offset(offset).limit(limit)
        )
    else:
        q = (
            select(Post)
            .where(Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.created_at))
            .offset(offset).limit(limit)
        )

    result = await db.execute(q)
    posts = result.scalars().all()
    enriched = await _enrich_posts(db, posts, user.id)

    return {"posts": enriched, "page": page, "limit": limit, "has_more": len(posts) == limit}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_post(
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    post = Post(
        author_id=user.id,
        content=data.get("content"),
        post_type=PostType(data.get("post_type", "text")),
        visibility=PostVisibility(data.get("visibility", "public")),
        media_urls=data.get("media_urls", []),
        code_snippet=data.get("code_snippet"),
        code_language=data.get("code_language"),
        hashtags=data.get("hashtags", []),
        poll_options=data.get("poll_options"),
    )
    db.add(post)
    await db.flush()
    await RealtimeService.on_post_created(db, post, user)
    return {
        "id": str(post.id),
        "message": "Post created",
        "post": {
            "id": str(post.id),
            "content": post.content,
            "post_type": post.post_type.value,
            "created_at": str(post.created_at),
            "author": {"id": str(user.id), "username": user.username,
                       "full_name": user.full_name, "avatar": user.avatar},
        },
    }


@router.patch("/{post_id}")
async def edit_post(
    post_id: str, data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Post).where(Post.id == UUID(post_id), Post.author_id == user.id)
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if data.get("content"):
        post.content = data["content"]
    if data.get("visibility"):
        post.visibility = PostVisibility(data["visibility"])
    post.is_edited = True
    return {"message": "Post updated"}


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Post).where(Post.id == UUID(post_id), Post.author_id == user.id)
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    await db.delete(post)
    await RealtimeService.on_post_deleted(db, UUID(post_id), user.id)


@router.get("/trending-posts")
async def get_trending_posts(
    limit: int = Query(10, ge=1, le=50),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Top posts by engagement in last 48h."""
    result = await db.execute(
        select(Post)
        .where(
            Post.visibility == PostVisibility.PUBLIC,
            Post.created_at >= func.now() - text("INTERVAL '48 hours'"),
        )
        .order_by(desc(Post.like_count + Post.comment_count + Post.repost_count))
        .limit(limit)
    )
    posts = result.scalars().all()
    return {"posts": await _enrich_posts(db, posts, user.id)}


@router.get("/recommended-posts")
async def get_recommended_posts(
    limit: int = Query(10, ge=1, le=50),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Recommended posts based on user's skills."""
    result = await db.execute(
        select(Post)
        .where(Post.visibility == PostVisibility.PUBLIC)
        .order_by(desc(Post.like_count))
        .limit(limit)
    )
    posts = result.scalars().all()
    return {"posts": await _enrich_posts(db, posts, user.id)}


@router.get("/bookmarks/me")
async def get_my_bookmarks(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * limit
    result = await db.execute(
        select(Post)
        .join(Bookmark, Bookmark.post_id == Post.id)
        .where(Bookmark.user_id == user.id)
        .order_by(desc(Bookmark.created_at))
        .offset(offset).limit(limit)
    )
    posts = result.scalars().all()
    return {"posts": await _enrich_posts(db, posts, user.id)}


@router.get("/{post_id}")
async def get_post(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Post).where(Post.id == UUID(post_id)))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    enriched = await _enrich_posts(db, [post], user.id)

    # Comments with authors — single IN query
    comments_result = await db.execute(
        select(Comment).where(Comment.post_id == post.id).order_by(Comment.created_at)
    )
    comments = comments_result.scalars().all()

    comment_author_ids = list({c.author_id for c in comments})
    if comment_author_ids:
        ca_result = await db.execute(select(User).where(User.id.in_(comment_author_ids)))
        ca_map = {u.id: u for u in ca_result.scalars().all()}
    else:
        ca_map = {}

    enriched_comments = [
        {
            "id":               str(c.id),
            "content":          c.content,
            "author_id":        str(c.author_id),
            "author_name":      ca_map.get(c.author_id, {}) and ca_map[c.author_id].full_name or "User",
            "author_avatar":    ca_map.get(c.author_id) and ca_map[c.author_id].avatar or None,
            "parent_comment_id":str(c.parent_comment_id) if c.parent_comment_id else None,
            "like_count":       c.like_count,
            "created_at":       str(c.created_at),
        }
        for c in comments
    ]

    return {"post": enriched[0], "comments": enriched_comments}


@router.get("/{post_id}/comments")
async def get_post_comments(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    pid = UUID(post_id)
    comments_result = await db.execute(
        select(Comment).where(Comment.post_id == pid).order_by(Comment.created_at)
    )
    comments = comments_result.scalars().all()

    author_ids = list({c.author_id for c in comments})
    if author_ids:
        au_result = await db.execute(select(User).where(User.id.in_(author_ids)))
        au_map = {u.id: u for u in au_result.scalars().all()}
    else:
        au_map = {}

    return {"comments": [
        {
            "id":                str(c.id),
            "content":           c.content,
            "author_id":         str(c.author_id),
            "author_name":       au_map.get(c.author_id) and au_map[c.author_id].full_name or "User",
            "author_username":   au_map.get(c.author_id) and au_map[c.author_id].username or "",
            "author_avatar":     au_map.get(c.author_id) and au_map[c.author_id].avatar or None,
            "parent_comment_id": str(c.parent_comment_id) if c.parent_comment_id else None,
            "like_count":        c.like_count,
            "created_at":        str(c.created_at),
        }
        for c in comments
    ]}


@router.post("/{post_id}/like", status_code=status.HTTP_201_CREATED)
async def like_post(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    pid = UUID(post_id)
    existing = await db.execute(
        select(Like).where(Like.user_id == user.id, Like.post_id == pid)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already liked")

    db.add(Like(user_id=user.id, post_id=pid))
    await db.execute(update(Post).where(Post.id == pid).values(like_count=Post.like_count + 1))
    await db.flush()

    post_result = await db.execute(select(Post).where(Post.id == pid))
    post = post_result.scalar_one()
    await RealtimeService.on_post_liked(db, post, user)
    await NotificationService.notify_like(db, post.author_id, user.id, pid, user.full_name)
    return {"message": "Liked", "like_count": post.like_count}


@router.delete("/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT)
async def unlike_post(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    pid = UUID(post_id)
    result = await db.execute(
        select(Like).where(Like.user_id == user.id, Like.post_id == pid)
    )
    like = result.scalar_one_or_none()
    if like:
        await db.delete(like)
        await db.execute(update(Post).where(Post.id == pid).values(
            like_count=func.greatest(Post.like_count - 1, 0)
        ))


@router.post("/{post_id}/comment", status_code=status.HTTP_201_CREATED)
async def add_comment(
    post_id: str, data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    pid = UUID(post_id)
    comment = Comment(
        post_id=pid,
        author_id=user.id,
        content=data["content"],
        parent_comment_id=UUID(data["parent_id"]) if data.get("parent_id") else None,
    )
    db.add(comment)
    await db.execute(update(Post).where(Post.id == pid).values(comment_count=Post.comment_count + 1))
    await db.flush()

    post_result = await db.execute(select(Post).where(Post.id == pid))
    post = post_result.scalar_one()
    await RealtimeService.on_post_commented(db, post, user, data["content"])
    await NotificationService.notify_comment(db, post.author_id, user.id, pid, user.full_name)
    return {"id": str(comment.id), "message": "Comment added", "comment_count": post.comment_count}


@router.delete("/{post_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    post_id: str, comment_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Comment).where(Comment.id == UUID(comment_id), Comment.author_id == user.id)
    )
    comment = result.scalar_one_or_none()
    if comment:
        await db.delete(comment)
        await db.execute(update(Post).where(Post.id == UUID(post_id)).values(
            comment_count=func.greatest(Post.comment_count - 1, 0)
        ))


@router.post("/{post_id}/bookmark", status_code=status.HTTP_201_CREATED)
async def bookmark_post(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    pid = UUID(post_id)
    existing = await db.execute(
        select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == pid)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already bookmarked")

    db.add(Bookmark(user_id=user.id, post_id=pid))
    await db.execute(update(Post).where(Post.id == pid).values(bookmark_count=Post.bookmark_count + 1))
    await db.flush()

    post_result = await db.execute(select(Post).where(Post.id == pid))
    post = post_result.scalar_one_or_none()
    if post and post.author_id != user.id:
        from app.websocket import ws_manager
        await ws_manager.send_to_user(str(post.author_id), {
            "type": "post_bookmarked",
            "data": {"post_id": str(pid), "bookmarker_name": user.full_name,
                     "bookmarker_avatar": user.avatar},
        })
    return {"message": "Bookmarked"}


@router.delete("/{post_id}/bookmark", status_code=status.HTTP_204_NO_CONTENT)
async def remove_bookmark(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    pid = UUID(post_id)
    result = await db.execute(
        select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == pid)
    )
    bm = result.scalar_one_or_none()
    if bm:
        await db.delete(bm)
        await db.execute(update(Post).where(Post.id == pid).values(
            bookmark_count=func.greatest(Post.bookmark_count - 1, 0)
        ))


@router.post("/{post_id}/repost", status_code=status.HTTP_201_CREATED)
async def repost(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    pid = UUID(post_id)
    existing = await db.execute(
        select(Post).where(Post.author_id == user.id, Post.parent_post_id == pid)
    )
    if existing.scalar_one_or_none():
        # Unrepost
        await db.delete(existing.scalar_one())
        await db.execute(update(Post).where(Post.id == pid).values(
            repost_count=func.greatest(Post.repost_count - 1, 0)
        ))
        return {"message": "Unreposted", "reposted": False}

    original = await db.execute(select(Post).where(Post.id == pid))
    original_post = original.scalar_one_or_none()
    if not original_post:
        raise HTTPException(status_code=404, detail="Post not found")

    repost_post = Post(
        author_id=user.id, content=None,
        post_type=original_post.post_type,
        visibility=PostVisibility.PUBLIC,
        parent_post_id=pid,
    )
    db.add(repost_post)
    await db.execute(update(Post).where(Post.id == pid).values(repost_count=Post.repost_count + 1))
    await db.flush()

    if original_post.author_id != user.id:
        from app.models import Notification, NotificationType
        from app.websocket import ws_manager
        db.add(Notification(
            user_id=original_post.author_id, actor_id=user.id,
            type=NotificationType.SYSTEM,
            title=f"{user.full_name} reposted your post",
            body=original_post.content[:80] if original_post.content else "",
            action_url="/feed",
        ))
        await ws_manager.send_to_user(str(original_post.author_id), {
            "type": "post_reposted",
            "data": {"post_id": str(pid), "reposter_name": user.full_name,
                     "reposter_avatar": user.avatar},
        })

    return {"id": str(repost_post.id), "message": "Reposted", "reposted": True}
