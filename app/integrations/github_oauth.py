"""GitHub OAuth integration."""

import httpx
from app.config import get_settings


async def exchange_github_code(code: str) -> dict:
    """Exchange authorization code for access token."""
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.GITHUB_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()


async def get_github_user(access_token: str) -> dict:
    """Fetch GitHub user profile."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        user = response.json()

        # Get primary email if not public
        if not user.get("email"):
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if emails_resp.status_code == 200:
                emails = emails_resp.json()
                primary = next((e for e in emails if e.get("primary")), None)
                if primary:
                    user["email"] = primary["email"]

        return user


async def get_github_repos(access_token: str, per_page: int = 100) -> list:
    """Fetch user repositories."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.github.com/user/repos?per_page={per_page}&sort=updated&type=owner",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()
