"""Feed endpoint tests."""

import pytest
from httpx import AsyncClient


async def get_auth_token(client: AsyncClient, email="feed@example.com") -> str:
    """Helper to register and get token."""
    reg = await client.post("/api/v1/auth/register", json={
        "email": email,
        "username": email.split("@")[0],
        "full_name": "Feed User",
        "password": "securepass123",
    })
    return reg.json()["access_token"]


@pytest.mark.asyncio
async def test_create_post(client: AsyncClient):
    """Test creating a post."""
    token = await get_auth_token(client)
    response = await client.post(
        "/api/v1/feed/",
        json={"content": "Hello GFD! #firstpost", "post_type": "text", "hashtags": ["firstpost"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    assert "id" in response.json()


@pytest.mark.asyncio
async def test_get_feed(client: AsyncClient):
    """Test getting feed."""
    token = await get_auth_token(client, "feedget@example.com")
    # Create a post first
    await client.post(
        "/api/v1/feed/",
        json={"content": "Test post"},
        headers={"Authorization": f"Bearer {token}"},
    )
    response = await client.get("/api/v1/feed/", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_like_post(client: AsyncClient):
    """Test liking a post."""
    token = await get_auth_token(client, "liker@example.com")
    post = await client.post(
        "/api/v1/feed/",
        json={"content": "Like me!"},
        headers={"Authorization": f"Bearer {token}"},
    )
    post_id = post.json()["id"]

    response = await client.post(
        f"/api/v1/feed/{post_id}/like",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_delete_post(client: AsyncClient):
    """Test deleting own post."""
    token = await get_auth_token(client, "deleter@example.com")
    post = await client.post(
        "/api/v1/feed/",
        json={"content": "Delete me"},
        headers={"Authorization": f"Bearer {token}"},
    )
    post_id = post.json()["id"]

    response = await client.delete(
        f"/api/v1/feed/{post_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204
