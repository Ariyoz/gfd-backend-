"""Feed & social endpoints â€” upgraded with link previews, hashtags, mentions, trending, rich media."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, update
from uuid import UUID
from typing import Optional
from datetime import datetime, timedelta, timezone

from app.database import get_db
from app.models import Post, Comment, Like, Bookmark, User, PostType, PostVisibility, Follow, Hashtag
from app.core.dependencies import get_current_active_user
from app.services.realtime import RealtimeService
from app.services.notification_service import NotificationService

router = APIRouter()


async def _enrich_posts(posts, user, db):
    """Enrich a list of posts with author info, interaction flags, and parent post for reposts."""
    enriched = []
    for post in posts:
        author_result = await db.execute(select(User).where(User.id == post.author_id))
        author = author_result.scalar_one_or_none()

        is_liked = False
        is_bookmarked = False
        if user:
            liked = await db.execute(select(Like).where(Like.user_id == user.id, Like.post_id == post.id))
            is_liked = liked.scalar_one_or_none() is not None
            bookmarked = await db.execute(select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == post.id))
            is_bookmarked = bookmarked.scalar_one_or_none() is not None

        # Repost: include parent post data
        parent_data = None
        if post.parent_post_id:
            parent_result = await db.execute(select(Post).where(Post.id == post.parent_post_id))
            parent = parent_result.scalar_one_or_none()
            if parent:
                parent_author_result = await db.execute(select(User).where(User.id == parent.author_id))
                parent_author = parent_author_result.scalar_one_or_none()
                parent_data = {
                    "id": str(parent.id),
                    "content": parent.content,
                    "post_type": parent.post_type.value if parent.post_type else "text",
                    "media_urls": parent.media_urls or [],
                    "video_url": parent.video_url,
                    "document_url": parent.document_url,
                    "document_name": parent.document_name,
                    "hashtags": parent.hashtags or [],
                    "link_preview": parent.link_preview,
                    "like_count": parent.like_count,
                    "comment_count": parent.comment_count,
                    "author": {
                        "id": str(parent_author.id) if parent_author else None,
                        "username": parent_author.username if parent_author else None,
                        "full_name": parent_author.full_name if parent_author else None,
                        "avatar": parent_author.avatar if parent_author else None,
                    } if parent_author else None,
                    "created_at": str(parent.created_at),
                }

        enriched.append({
            "id": str(post.id),
            "content": post.content,
            "post_type": post.post_type.value if post.post_type else "text",
            "media_urls": post.media_urls or [],
            "video_url": post.video_url,
            "document_url": post.document_url,
            "document_name": post.document_name,
            "code_snippet": post.code_snippet,
            "code_language": post.code_language,
            "hashtags": post.hashtags or [],
            "mentions": [str(m) for m in (post.mentions or [])],
            "link_preview": post.link_preview,
            "like_count": post.like_count,
            "comment_count": post.comment_count,
            "repost_count": post.repost_count,
            "bookmark_count": post.bookmark_count,
            "is_liked": is_liked,
            "is_bookmarked": is_bookmarked,
            "is_edited": post.is_edited,
            "parent_post_id": str(post.parent_post_id) if post.parent_post_id else None,
            "parent_post": parent_data,
            "created_at": str(post.created_at),
            "author": {
                "id": str(author.id) if author else None,
                "username": author.username if author else None,
                "full_name": author.full_name if author else None,
                "avatar": author.avatar if author else None,
                "is_verified": author.is_verified if author else False,
            } if author else None,
        })
    return enriched


@router.get("/")
async def get_feed(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    feed_type: str = Query("explore", description="following, explore, or user"),
    user_id: Optional[str] = Query(None, description="For user-specific feed"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get feed â€” following, explore, or user-specific."""
    offset = (page - 1) * limit

    if feed_type == "following" and user:
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
        query = (
            select(Post)
            .where(Post.author_id == UUID(user_id), Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.created_at))
            .offset(offset)
            .limit(limit)
        )
    else:
        query = (
            select(Post)
            .where(Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.created_at))
            .offset(offset)
            .limit(limit)
        )

    result = await db.execute(query)
    posts = result.scalars().all()
    enriched = await _enrich_posts(posts, user, db)
    return {"posts": enriched, "page": page, "limit": limit, "has_more": len(posts) == limit}


@router.get("/trending-posts")
async def get_trending_posts(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Top 10 posts by interaction score in last 24 hours, cached 15 min."""
    from app.services.cache import CacheService

    cache_key = "feed:trending_posts"
    cached = await CacheService.get(cache_key)
    if cached:
        return {"posts": cached}

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await db.execute(
        select(Post)
        .where(Post.visibility == PostVisibility.PUBLIC, Post.created_at >= since)
        .order_by(desc(Post.like_count + Post.comment_count + Post.repost_count), desc(Post.created_at))
        .limit(10)
    )
    posts = result.scalars().all()
    enriched = await _enrich_posts(posts, user, db)
    await CacheService.set(cache_key, enriched, ttl=900)  # 15 min
    return {"posts": enriched}


@router.get("/recommended-posts")
async def get_recommended_posts(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Recommended posts based on user skill tags and followed users, cached 15 min."""
    from app.services.cache import CacheService
    from app.models import DeveloperProfile

    cache_key = f"feed:recommended:{user.id}"
    cached = await CacheService.get(cache_key)
    if cached:
        return {"posts": cached}

    # Get user's skills
    dev_profile = await db.execute(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
    dev = dev_profile.scalar_one_or_none()
    skills = dev.skills or [] if dev else []

    # Get followed user IDs
    following_result = await db.execute(select(Follow.following_id).where(Follow.follower_id == user.id))
    following_ids = [row[0] for row in following_result.fetchall()]

    posts = []

    # Posts tagged with user's skills
    if skills:
        result = await db.execute(
            select(Post)
            .where(
                Post.visibility == PostVisibility.PUBLIC,
                Post.hashtags.overlap(skills),
                Post.author_id != user.id,
            )
            .order_by(desc(Post.created_at))
            .limit(10)
        )
        posts.extend(result.scalars().all())

    # Posts from followed users (if any)
    if following_ids:
        existing_ids = [p.id for p in posts]
        result = await db.execute(
            select(Post)
            .where(
                Post.visibility == PostVisibility.PUBLIC,
                Post.author_id.in_(following_ids),
                Post.id.notin_(existing_ids) if existing_ids else True,
            )
            .order_by(desc(Post.created_at))
            .limit(10)
        )
        posts.extend(result.scalars().all())

    if not posts:
        # Fallback: popular public posts
        result = await db.execute(
            select(Post)
            .where(Post.visibility == PostVisibility.PUBLIC)
            .order_by(desc(Post.like_count + Post.comment_count), desc(Post.created_at))
            .limit(10)
        )
        posts = result.scalars().all()

    enriched = await _enrich_posts(posts[:10], user, db)
    await CacheService.set(cache_key, enriched, ttl=900)
    return {"posts": enriched}


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_post(
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new post â€” extracts hashtags/mentions, generates link preview, broadcasts."""
    from app.services.link_preview import fetch_link_preview, extract_urls, extract_hashtags, extract_mentions

    content = data.get("content", "")

    # Extract hashtags and mentions
    raw_hashtags = data.get("hashtags", []) or extract_hashtags(content)
    raw_mentions = extract_mentions(content)

    # Resolve mentions to UUIDs
    mention_ids = []
    mention_notifications = []
    for username in raw_mentions:
        result = await db.execute(select(User).where(User.username == username))
        mentioned_user = result.scalar_one_or_none()
        if mentioned_user:
            mention_ids.append(mentioned_user.id)
            mention_notifications.append(mentioned_user)

    # Update hashtag counts
    for tag in raw_hashtags:
        ht_result = await db.execute(select(Hashtag).where(Hashtag.name == tag))
        ht = ht_result.scalar_one_or_none()
        if ht:
            ht.post_count += 1
        else:
            db.add(Hashtag(name=tag, post_count=1))

    # Extract link preview from first URL
    link_preview = None
    if content:
        urls = extract_urls(content)
        if urls:
            link_preview = await fetch_link_preview(urls[0])

    post = Post(
        author_id=user.id,
        content=content,
        post_type=PostType(data.get("post_type", "text")),
        visibility=PostVisibility(data.get("visibility", "public")),
        media_urls=data.get("media_urls", []),
        video_url=data.get("video_url"),
        document_url=data.get("document_url"),
        document_name=data.get("document_name"),
        code_snippet=data.get("code_snippet"),
        code_language=data.get("code_language"),
        hashtags=raw_hashtags,
        mentions=mention_ids,
        link_preview=link_preview,
        poll_options=data.get("poll_options"),
    )
    db.add(post)
    await db.flush()

    # Real-time broadcast to followers
    await RealtimeService.on_post_created(db, post, user)

    # Mention notifications
    for mentioned in mention_notifications:
        if mentioned.id != user.id:
            await NotificationService.create(
                db=db,
                user_id=mentioned.id,
                actor_id=user.id,
                type=__import__("app.models", fromlist=["NotificationType"]).NotificationType.MENTION,
                title=f"{user.full_name} mentioned you in a post",
                data={"post_id": str(post.id)},
                action_url=f"/feed/{post.id}",
            )

    return {
        "id": str(post.id),
        "message": "Post created",
        "post": {
            "id": str(post.id),
            "content": post.content,
            "post_type": post.post_type.value,
            "hashtags": post.hashtags or [],
            "link_preview": link_preview,
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
async def edit_post(
    post_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
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
async def delete_post(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete own post with real-time sync."""
    result = await db.execute(select(Post).where(Post.id == UUID(post_id), Post.author_id == user.id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    await db.delete(post)
    await RealtimeService.on_post_deleted(db, UUID(post_id), user.id)


@router.get("/{post_id}")
async def get_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get a single post with enriched data and comments."""
    result = await db.execute(select(Post).where(Post.id == UUID(post_id)))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    enriched = await _enrich_posts([post], user, db)

    comments_result = await db.execute(
        select(Comment).where(Comment.post_id == post.id).order_by(Comment.created_at)
    )
    comments = comments_result.scalars().all()

    enriched_comments = []
    for c in comments:
        comment_author_result = await db.execute(select(User).where(User.id == c.author_id))
        comment_author = comment_author_result.scalar_one_or_none()
        enriched_comments.append({
            "id": str(c.id),
            "content": c.content,
            "author_id": str(c.author_id),
            "author_name": comment_author.full_name if comment_author else "User",
            "author_username": comment_author.username if comment_author else "",
            "author_avatar": comment_author.avatar if comment_author else None,
            "parent_comment_id": str(c.parent_comment_id) if c.parent_comment_id else None,
            "like_count": c.like_count,
            "created_at": str(c.created_at),
        })

    return {"post": enriched[0] if enriched else None, "comments": enriched_comments}


@router.post("/{post_id}/like", status_code=status.HTTP_201_CREATED)
async def like_post(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Like a post â€” instant count update + notification."""
    pid = UUID(post_id)
    existing = await db.execute(select(Like).where(Like.user_id == user.id, Like.post_id == pid))
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
    """Unlike a post."""
    pid = UUID(post_id)
    result = await db.execute(select(Like).where(Like.user_id == user.id, Like.post_id == pid))
    like = result.scalar_one_or_none()
    if like:
        await db.delete(like)
        await db.execute(update(Post).where(Post.id == pid).values(like_count=Post.like_count - 1))


@router.post("/{post_id}/comment", status_code=status.HTTP_201_CREATED)
async def add_comment(
    post_id: str,
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a comment â€” supports nested replies, triggers mention notifications."""
    from app.services.link_preview import extract_mentions

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

    # Mention notifications in comments
    from app.models import NotificationType as NT
    for username in extract_mentions(data["content"]):
        mentioned_result = await db.execute(select(User).where(User.username == username))
        mentioned = mentioned_result.scalar_one_or_none()
        if mentioned and mentioned.id != user.id and mentioned.id != post.author_id:
            await NotificationService.create(
                db=db,
                user_id=mentioned.id,
                actor_id=user.id,
                type=NT.MENTION,
                title=f"{user.full_name} mentioned you in a comment",
                data={"post_id": str(pid)},
                action_url=f"/feed/{pid}",
            )

    return {"id": str(comment.id), "message": "Comment added", "comment_count": post.comment_count}


@router.post("/{post_id}/bookmark", status_code=status.HTTP_201_CREATED)
async def bookmark_post(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Bookmark a post."""
    pid = UUID(post_id)
    existing = await db.execute(select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == pid))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Already bookmarked")
    db.add(Bookmark(user_id=user.id, post_id=pid))
    await db.execute(update(Post).where(Post.id == pid).values(bookmark_count=Post.bookmark_count + 1))
    await db.flush()
    return {"message": "Bookmarked"}


@router.delete("/{post_id}/bookmark", status_code=status.HTTP_204_NO_CONTENT)
async def remove_bookmark(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove bookmark."""
    pid = UUID(post_id)
    result = await db.execute(select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.post_id == pid))
    bm = result.scalar_one_or_none()
    if bm:
        await db.delete(bm)
        await db.execute(update(Post).where(Post.id == pid).values(bookmark_count=Post.bookmark_count - 1))


@router.post("/{post_id}/repost", status_code=status.HTTP_201_CREATED)
async def repost(
    post_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Repost a post â€” toggle. Creates child Post with parent_post_id."""
    pid = UUID(post_id)

    existing = await db.execute(select(Post).where(Post.author_id == user.id, Post.parent_post_id == pid))
    existing_repost = existing.scalar_one_or_none()

    if existing_repost:
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

    if original_post.author_id != user.id:
        from app.models import Notification, NotificationType
        db.add(Notification(
            user_id=original_post.author_id,
            actor_id=user.id,
            type=NotificationType.REPOST,
            title=f"{user.full_name} reposted your post",
            body=(original_post.content or "")[:80],
            action_url="/feed",
        ))
        await ws_manager_send(original_post.author_id, user, pid, repost_post.id)

    return {"id": str(repost_post.id), "message": "Reposted", "reposted": True}


async def ws_manager_send(author_id, user, pid, repost_id):
    from app.websocket import ws_manager
    await ws_manager.send_to_user(str(author_id), {
        "type": "post_reposted",
        "data": {
            "post_id": str(pid),
            "repost_id": str(repost_id),
            "reposter_name": user.full_name,
            "reposter_avatar": user.avatar,
        },
    })


@router.delete("/{post_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    post_id: str,
    comment_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a comment (comment author or post author)."""
    result = await db.execute(select(Comment).where(Comment.id == UUID(comment_id)))
    comment = result.scalar_one_or_none()
    if comment and (comment.author_id == user.id):
        await db.delete(comment)
        await db.execute(update(Post).where(Post.id == UUID(post_id)).values(comment_count=Post.comment_count - 1))


@router.get("/{post_id}/comments")
async def get_post_comments(
    post_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get all comments for a post with author info and threaded replies."""
    pid = UUID(post_id)
    comments_result = await db.execute(
        select(Comment).where(Comment.post_id == pid).order_by(Comment.created_at)
    )
    comments = comments_result.scalars().all()

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
    posts = result.scalars().all()
    enriched = await _enrich_posts(posts, user, db)
    return {"posts": enriched}

