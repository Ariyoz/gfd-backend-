"""FastAPI dependencies — auth, permissions, rate limiting."""

from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, UserRole
from app.core.security import decode_token

security_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate the current user from JWT token."""
    token = credentials.credentials
    payload = decode_token(token)

    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if user.status.value == "suspended":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")

    return user


async def get_current_active_user(user: User = Depends(get_current_user)) -> User:
    """Ensure user is active. Admins bypass status check."""
    if user.role.value == "admin":
        return user  # Admins always allowed regardless of status
    if user.status.value not in ("active", "pending_verification"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account not active")
    return user


def require_role(*roles: UserRole):
    """Dependency factory for role-based access control."""
    async def role_checker(user: User = Depends(get_current_active_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {[r.value for r in roles]}"
            )
        return user
    return role_checker


# Convenience role dependencies
require_admin = require_role(UserRole.ADMIN)
require_developer = require_role(UserRole.DEVELOPER)
require_client = require_role(UserRole.CLIENT)
require_developer_or_client = require_role(UserRole.DEVELOPER, UserRole.CLIENT)
