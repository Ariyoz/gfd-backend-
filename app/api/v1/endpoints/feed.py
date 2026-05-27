"""Feed & social endpoints — posts, likes, comments, bookmarks with real-time sync."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, update
from uuid import UUID
from typing import Optional

from app.database import get_db
from app.models import Post, Comment, Like, Bookmark, User, PostType, PostVisibility, Follow
from app.core.dependencies import get_current_active_user
from app.services.realtime import RealtimeService
from app.services.notification_service import NotificationService

router = APIRouter()


@router.get("/")
async def get_feed(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    feed_type: str = Query("explore", description="following, explore, or user"),
    user_id: Optional[str] = Query(None, description="For user-specific feed"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get feed — following, explore, or user-specific."""
    offset = (page - 1) * limit

    if feed_type == "following" and user:
        # Get posts from followed users + own posts
        following_result = await db.execute(
            select(Follow.following_id).where(Follow.follower_id == user.id)
        )
        following_ids = [row[0] for row in following_result.fetchall()]
        following_ids.append(user.id)

        query = (
            select(Post)
            .where(Post.author_id.in_(following_ids), Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.created_at))
            .offset(offset)
            .limit(limit)
        )

    elif feed_type == "user" and user_id:
        # Specific user's posts
        query = (
            select(Post)
            .where(Post.author_id == UUID(user_id), Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.created_at))
            .offset(offset)
            .limit(limit)
        )
    else:
        # Explore/global feed — ALL public posts, newest first
        query = (
            select(Post)
            .where(Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.created_at))
            .offset(offset)
            .limit(limit)
        )

    result = await db.execute(query)
    posts = result.scalars().all()

    # Enrich posts with author info
    enriched = []
    for post in posts:
        author_result = await db.execute(select(User).where(User.id == post.author_id))
        author = author_result.scalar_one_or_none()

        # Check if current user liked this post
        liked = await db.execute(select(Like).where(Like.user_id == user.id, Like.post_id == post.id))
        is_liked = liked.scalar_one_or_none() is not None

        # Check if bookmarked
        bookmarked = await db.execute(select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == post.id))
        is_bookmarked = bookmarked.scalar_one_or_none() is not None

        enriched.append({
            "id": str(post.id),
            "content": post.content,
            "post_type": post.post_type.value if post.post_type else "text",
            "media_urls": post.media_urls or [],
            "code_snippet": post.code_snippet,
            "code_language": post.code_language,
            "hashtags": post.hashtags or [],
            "like_count": post.like_count,
            "comment_count": post.comment_count,
            "repost_count": post.repost_count,
            "bookmark_count": post.bookmark_count,
            "is_liked": is_liked,
            "is_bookmarked": is_bookmarked,
            "is_edited": post.is_edited,
            "created_at": str(post.created_at),
            "author": {
                "id": str(author.id) if author else None,
                "username": author.username if author else None,
                "full_name": author.full_name if author else None,
                "avatar": author.avatar if author else None,
                "is_verified": author.is_verified if author else False,
            } if author else None,
        })

    return {"posts": enriched, "page": page, "limit": limit, "has_more": len(posts) == limit}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_post(data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Create a new post with real-time broadcasting."""
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

    # Real-time broadcast to followers
    await RealtimeService.on_post_created(db, post, user)

    return {
        "id": str(post.id),
        "message": "Post created",
        "post": {
            "id": str(post.id),
            "content": post.content,
            "post_type": post.post_type.value,
            "created_at": str(post.created_at),
            "author": {
                "id": str(user.id),
                "username": user.username,
                "full_name": user.full_name,
                "avatar": user.avatar,
            },
        },
    }


@router.patch("/{post_id}")
async def edit_post(post_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Edit own post."""
    result = await db.execute(select(Post).where(Post.id == UUID(post_id), Post.author_id == user.id))
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
async def delete_post(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Delete own post with real-time sync."""
    result = await db.execute(select(Post).where(Post.id == UUID(post_id), Post.author_id == user.id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    await db.delete(post)
    await RealtimeService.on_post_deleted(db, UUID(post_id), user.id)


@router.get("/{post_id}")
async def get_post(post_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single post with comments."""
    result = await db.execute(select(Post).where(Post.id == UUID(post_id)))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Get author
    author_result = await db.execute(select(User).where(User.id == post.author_id))
    author = author_result.scalar_one_or_none()

    # Get comments with author info
    comments_result = await db.execute(
        select(Comment).where(Comment.post_id == post.id).order_by(Comment.created_at)
    )
    comments = comments_result.scalars().all()

    # Enrich comments with author info
    enriched_comments = []
    for c in comments:
        comment_author_result = await db.execute(select(User).where(User.id == c.author_id))
        comment_author = comment_author_result.scalar_one_or_none()
        enriched_comments.append({
            "id": str(c.id),
            "content": c.content,
            "author_id": str(c.author_id),
            "author_name": comment_author.full_name if comment_author else "User",
            "author_avatar": comment_author.avatar if comment_author else None,
            "parent_comment_id": str(c.parent_comment_id) if c.parent_comment_id else None,
            "like_count": c.like_count,
            "created_at": str(c.created_at),
        })

    return {
        "post": {
            "id": str(post.id),
            "content": post.content,
            "post_type": post.post_type.value if post.post_type else "text",
            "media_urls": post.media_urls or [],
            "hashtags": post.hashtags or [],
            "like_count": post.like_count,
            "comment_count": post.comment_count,
            "repost_count": post.repost_count,
            "created_at": str(post.created_at),
            "author": {
                "id": str(author.id),
                "username": author.username,
                "full_name": author.full_name,
                "avatar": author.avatar,
            } if author else None,
        },
        "comments": enriched_comments,
    }


@router.post("/{post_id}/like", status_code=status.HTTP_201_CREATED)
async def like_post(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Like a post — instant count update + notification."""
    pid = UUID(post_id)
    existing = await db.execute(select(Like).where(Like.user_id == user.id, Like.post_id == pid))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already liked")

    db.add(Like(user_id=user.id, post_id=pid))

    # Increment like count
    await db.execute(update(Post).where(Post.id == pid).values(like_count=Post.like_count + 1))
    await db.flush()

    # Get post for notification
    post_result = await db.execute(select(Post).where(Post.id == pid))
    post = post_result.scalar_one()

    # Real-time broadcast
    await RealtimeService.on_post_liked(db, post, user)

    # Notification
    await NotificationService.notify_like(db, post.author_id, user.id, pid, user.full_name)

    return {"message": "Liked", "like_count": post.like_count}


@router.delete("/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT)
async def unlike_post(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Unlike a post."""
    pid = UUID(post_id)
    result = await db.execute(select(Like).where(Like.user_id == user.id, Like.post_id == pid))
    like = result.scalar_one_or_none()
    if like:
        await db.delete(like)
        await db.execute(update(Post).where(Post.id == pid).values(like_count=Post.like_count - 1))


@router.post("/{post_id}/comment", status_code=status.HTTP_201_CREATED)
async def add_comment(post_id: str, data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Add a comment — supports nested replies."""
    pid = UUID(post_id)
    comment = Comment(
        post_id=pid,
        author_id=user.id,
        content=data["content"],
        parent_comment_id=UUID(data["parent_id"]) if data.get("parent_id") else None,
    )
    db.add(comment)

    # Increment comment count
    await db.execute(update(Post).where(Post.id == pid).values(comment_count=Post.comment_count + 1))
    await db.flush()

    # Get post for notification
    post_result = await db.execute(select(Post).where(Post.id == pid))
    post = post_result.scalar_one()

    # Real-time + notification
    await RealtimeService.on_post_commented(db, post, user, data["content"])
    await NotificationService.notify_comment(db, post.author_id, user.id, pid, user.full_name)

    return {"id": str(comment.id), "message": "Comment added", "comment_count": post.comment_count}


@router.post("/{post_id}/bookmark", status_code=status.HTTP_201_CREATED)
async def bookmark_post(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Bookmark a post."""
    pid = UUID(post_id)
    existing = await db.execute(select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == pid))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already bookmarked")
    db.add(Bookmark(user_id=user.id, post_id=pid))
    await db.execute(update(Post).where(Post.id == pid).values(bookmark_count=Post.bookmark_count + 1))
    await db.flush()

    # Notify post author via WebSocket
    post_result = await db.execute(select(Post).where(Post.id == pid))
    post = post_result.scalar_one_or_none()
    if post and post.author_id != user.id:
        from app.websocket import ws_manager
        await ws_manager.send_to_user(str(post.author_id), {
            "type": "post_bookmarked",
            "data": {
                "post_id": str(pid),
                "bookmarker_name": user.full_name,
                "bookmarker_avatar": user.avatar,
            },
        })

    return {"message": "Bookmarked"}


@router.delete("/{post_id}/bookmark", status_code=status.HTTP_204_NO_CONTENT)
async def remove_bookmark(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Remove bookmark."""
    pid = UUID(post_id)
    result = await db.execute(select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == pid))
    bm = result.scalar_one_or_none()
    if bm:
        await db.delete(bm)
        await db.execute(update(Post).where(Post.id == pid).values(bookmark_count=Post.bookmark_count - 1))


@router.post("/{post_id}/repost", status_code=status.HTTP_201_CREATED)
async def repost(post_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Repost a post — one per user. If already reposted, unrepost."""
    pid = UUID(post_id)

    # Check if already reposted
    existing = await db.execute(select(Post).where(Post.author_id == user.id, Post.parent_post_id == pid))
    existing_repost = existing.scalar_one_or_none()

    if existing_repost:
        # Unrepost
        await db.delete(existing_repost)
        await db.execute(update(Post).where(Post.id == pid).values(repost_count=Post.repost_count - 1))
        return {"message": "Unreposted", "reposted": False}

    original = await db.execute(select(Post).where(Post.id == pid))
    original_post = original.scalar_one_or_none()
    if not original_post:
        raise HTTPException(status_code=404, detail="Post not found")

    repost_post = Post(
        author_id=user.id,
        content=None,
        post_type=original_post.post_type,
        visibility=PostVisibility.PUBLIC,
        parent_post_id=pid,
    )
    db.add(repost_post)
    await db.execute(update(Post).where(Post.id == pid).values(repost_count=Post.repost_count + 1))
    await db.flush()

    # Notify original post author via WebSocket + DB notification
    if original_post.author_id != user.id:
        from app.models import Notification, NotificationType
        from app.websocket import ws_manager
        db.add(Notification(
            user_id=original_post.author_id,
            actor_id=user.id,
            type=NotificationType.SYSTEM,
            title=f"{user.full_name} reposted your post",
            body=original_post.content[:80] if original_post.content else "",
            action_url="/feed",
        ))
        # Send instant WebSocket notification
        await ws_manager.send_to_user(str(original_post.author_id), {
            "type": "post_reposted",
            "data": {
                "post_id": str(pid),
                "reposter_name": user.full_name,
                "reposter_avatar": user.avatar,
            },
        })

    return {"id": str(repost_post.id), "message": "Reposted", "reposted": True}


@router.delete("/{post_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(post_id: str, comment_id: str, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Delete a comment (only comment author can delete)."""
    result = await db.execute(select(Comment).where(Comment.id == UUID(comment_id), Comment.author_id == user.id))
    comment = result.scalar_one_or_none()
    if comment:
        await db.delete(comment)
        await db.execute(update(Post).where(Post.id == UUID(post_id)).values(comment_count=Post.comment_count - 1))


@router.get("/{post_id}/comments")
async def get_post_comments(post_id: str, db: AsyncSession = Depends(get_db)):
    """Get all comments for a post with author info and threaded replies."""
    pid = UUID(post_id)

    # Get all comments for this post
    comments_result = await db.execute(
        select(Comment).where(Comment.post_id == pid).order_by(Comment.created_at)
    )
    comments = comments_result.scalars().all()

    # Enrich with author info
    enriched = []
    for c in comments:
        author_result = await db.execute(select(User).where(User.id == c.author_id))
        author = author_result.scalar_one_or_none()
        enriched.append({
            "id": str(c.id),
            "content": c.content,
            "author_id": str(c.author_id),
            "author_name": author.full_name if author else "User",
            "author_username": author.username if author else "",
            "author_avatar": author.avatar if author else None,
            "parent_comment_id": str(c.parent_comment_id) if c.parent_comment_id else None,
            "like_count": c.like_count,
            "created_at": str(c.created_at),
        })

    return {"comments": enriched}


@router.get("/bookmarks/me")
async def get_my_bookmarks(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's bookmarked posts."""
    offset = (page - 1) * limit
    result = await db.execute(
        select(Post)
        .join(Bookmark, Bookmark.post_id == Post.id)
        .where(Bookmark.user_id == user.id)
        .order_by(desc(Bookmark.created_at))
        .offset(offset)
        .limit(limit)
    )
    return {"posts": result.scalars().all()}
