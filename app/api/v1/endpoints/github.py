"""GitHub integration endpoints — sync profile and repos."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID

from app.database import get_db
from app.models import User, DeveloperProfile, GitHubProfile, Repository, OAuthAccount
from app.core.dependencies import get_current_active_user
from app.integrations.github_oauth import get_github_user, get_github_repos

router = APIRouter()


@router.post("/sync")
async def sync_github(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Sync GitHub profile and repositories for current user."""
    # Get OAuth token
    result = await db.execute(
        select(OAuthAccount).where(OAuthAccount.user_id == user.id, OAuthAccount.provider == "github")
    )
    oauth = result.scalar_one_or_none()
    if not oauth or not oauth.access_token:
        raise HTTPException(status_code=400, detail="GitHub account not connected")

    # Fetch from GitHub API
    gh_user = await get_github_user(oauth.access_token)
    gh_repos = await get_github_repos(oauth.access_token)

    # Get developer profile
    dev_result = await db.execute(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
    dev_profile = dev_result.scalar_one_or_none()
    if not dev_profile:
        raise HTTPException(status_code=400, detail="Developer profile not found")

    # Upsert GitHub profile
    gh_profile_result = await db.execute(select(GitHubProfile).where(GitHubProfile.developer_id == dev_profile.id))
    gh_profile = gh_profile_result.scalar_one_or_none()

    profile_data = {
        "github_id": str(gh_user["id"]),
        "username": gh_user["login"],
        "avatar_url": gh_user.get("avatar_url"),
        "bio": gh_user.get("bio"),
        "followers": gh_user.get("followers", 0),
        "following": gh_user.get("following", 0),
        "public_repos": gh_user.get("public_repos", 0),
        "profile_url": gh_user.get("html_url"),
        "blog": gh_user.get("blog"),
        "company": gh_user.get("company"),
        "location": gh_user.get("location"),
        "hireable": gh_user.get("hireable"),
    }

    if gh_profile:
        for key, value in profile_data.items():
            setattr(gh_profile, key, value)
    else:
        gh_profile = GitHubProfile(developer_id=dev_profile.id, **profile_data)
        db.add(gh_profile)
        await db.flush()

    # Sync repositories
    # Delete old repos and re-insert
    old_repos = await db.execute(select(Repository).where(Repository.github_profile_id == gh_profile.id))
    for repo in old_repos.scalars().all():
        await db.delete(repo)

    for repo_data in gh_repos[:100]:  # Limit to 100 repos
        repo = Repository(
            github_profile_id=gh_profile.id,
            github_repo_id=str(repo_data["id"]),
            name=repo_data["name"],
            full_name=repo_data.get("full_name"),
            description=repo_data.get("description"),
            language=repo_data.get("language"),
            stars=repo_data.get("stargazers_count", 0),
            forks=repo_data.get("forks_count", 0),
            watchers=repo_data.get("watchers_count", 0),
            open_issues=repo_data.get("open_issues_count", 0),
            is_fork=repo_data.get("fork", False),
            is_private=repo_data.get("private", False),
            topics=repo_data.get("topics", []),
            repo_url=repo_data.get("html_url", ""),
            homepage=repo_data.get("homepage"),
            default_branch=repo_data.get("default_branch", "main"),
            last_pushed_at=repo_data.get("pushed_at"),
        )
        db.add(repo)

    return {"message": "GitHub synced", "repos_synced": len(gh_repos[:100])}


@router.get("/profile")
async def get_github_profile(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get synced GitHub profile."""
    dev_result = await db.execute(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
    dev_profile = dev_result.scalar_one_or_none()
    if not dev_profile:
        raise HTTPException(status_code=404, detail="Developer profile not found")

    result = await db.execute(select(GitHubProfile).where(GitHubProfile.developer_id == dev_profile.id))
    gh_profile = result.scalar_one_or_none()
    if not gh_profile:
        raise HTTPException(status_code=404, detail="GitHub not synced yet")
    return gh_profile


@router.get("/repos")
async def get_repos(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get synced repositories."""
    dev_result = await db.execute(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
    dev_profile = dev_result.scalar_one_or_none()
    if not dev_profile:
        return {"repositories": []}

    gh_result = await db.execute(select(GitHubProfile).where(GitHubProfile.developer_id == dev_profile.id))
    gh_profile = gh_result.scalar_one_or_none()
    if not gh_profile:
        return {"repositories": []}

    result = await db.execute(select(Repository).where(Repository.github_profile_id == gh_profile.id).order_by(Repository.stars.desc()))
    return {"repositories": result.scalars().all()}
