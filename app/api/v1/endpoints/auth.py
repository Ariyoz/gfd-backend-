"""Authentication endpoints — login, register, OAuth, tokens."""

from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, UserRole, UserStatus, OAuthAccount, Session, DeveloperProfile, ClientProfile
from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token,
    create_verification_token, create_password_reset_token,
)
from app.schemas.auth import (
    RegisterRequest, LoginRequest, TokenResponse,
    RefreshTokenRequest, ForgotPasswordRequest, ResetPasswordRequest,
)

router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new user with email/password."""
    # Check existing
    existing = await db.execute(select(User).where((User.email == data.email) | (User.username == data.username)))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email or username already taken")

    user = User(
        email=data.email,
        username=data.username,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=UserRole(data.role) if data.role else UserRole.DEVELOPER,
        status=UserStatus.ACTIVE,
        is_verified=False,
    )
    db.add(user)
    await db.flush()

    # Create role-specific profile
    if user.role == UserRole.DEVELOPER:
        db.add(DeveloperProfile(user_id=user.id))
    elif user.role == UserRole.CLIENT:
        db.add(ClientProfile(user_id=user.id))

    await db.flush()

    # Generate tokens
    access_token = create_access_token(user.id, user.role.value)
    refresh_token = create_refresh_token(user.id)

    # Store session
    db.add(Session(user_id=user.id, refresh_token=refresh_token, expires_at="", is_active=True))

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user_id=str(user.id),
        role=user.role.value,
    )


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Login with email/password."""
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    if not user or not user.hashed_password or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if user.status == UserStatus.SUSPENDED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")

    access_token = create_access_token(user.id, user.role.value)
    refresh_token = create_refresh_token(user.id)

    db.add(Session(user_id=user.id, refresh_token=refresh_token, expires_at="", is_active=True))

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user_id=str(user.id),
        role=user.role.value,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(data: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """Refresh access token using refresh token."""
    payload = decode_token(data.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    # Verify session exists
    result = await db.execute(
        select(Session).where(Session.refresh_token == data.refresh_token, Session.is_active == True)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or revoked")

    # Get user
    result = await db.execute(select(User).where(User.id == session.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Rotate tokens
    session.is_active = False
    new_access = create_access_token(user.id, user.role.value)
    new_refresh = create_refresh_token(user.id)
    db.add(Session(user_id=user.id, refresh_token=new_refresh, expires_at="", is_active=True))

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        token_type="bearer",
        user_id=str(user.id),
        role=user.role.value,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """Revoke refresh token (logout)."""
    result = await db.execute(select(Session).where(Session.refresh_token == data.refresh_token))
    session = result.scalar_one_or_none()
    if session:
        session.is_active = False


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(data: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Send password reset email."""
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if user:
        token = create_password_reset_token(user.email)
        # TODO: Send email via Resend (celery task)
    return {"message": "If the email exists, a reset link has been sent."}


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(data: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Reset password using token."""
    payload = decode_token(data.token)
    if not payload or payload.get("type") != "password_reset":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token")

    email = payload.get("sub")
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.hashed_password = hash_password(data.new_password)
    return {"message": "Password reset successfully"}


# ── OAuth Endpoints ──

@router.get("/github/login")
async def github_login():
    """Redirect to GitHub OAuth."""
    from app.config import get_settings
    s = get_settings()
    url = f"https://github.com/login/oauth/authorize?client_id={s.GITHUB_CLIENT_ID}&redirect_uri={s.GITHUB_REDIRECT_URI}&scope=read:user,user:email"
    return {"url": url}


@router.get("/github/callback")
async def github_callback(code: str, db: AsyncSession = Depends(get_db)):
    """Handle GitHub OAuth callback."""
    from app.integrations.github_oauth import exchange_github_code, get_github_user
    token_data = await exchange_github_code(code)
    github_user = await get_github_user(token_data["access_token"])

    # Find or create user
    result = await db.execute(
        select(OAuthAccount).where(OAuthAccount.provider == "github", OAuthAccount.provider_user_id == str(github_user["id"]))
    )
    oauth = result.scalar_one_or_none()

    if oauth:
        user_result = await db.execute(select(User).where(User.id == oauth.user_id))
        user = user_result.scalar_one()
        oauth.access_token = token_data["access_token"]
    else:
        # Create new user
        user = User(
            email=github_user.get("email") or f"{github_user['login']}@github.oauth",
            username=github_user["login"],
            full_name=github_user.get("name") or github_user["login"],
            avatar=github_user.get("avatar_url"),
            role=UserRole.DEVELOPER,
            status=UserStatus.ACTIVE,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        db.add(DeveloperProfile(user_id=user.id, github_url=github_user.get("html_url")))
        db.add(OAuthAccount(
            user_id=user.id,
            provider="github",
            provider_user_id=str(github_user["id"]),
            access_token=token_data["access_token"],
        ))
        await db.flush()

    access_token = create_access_token(user.id, user.role.value)
    refresh_token = create_refresh_token(user.id)
    db.add(Session(user_id=user.id, refresh_token=refresh_token, expires_at="", is_active=True))

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user_id=str(user.id),
        role=user.role.value,
    )


@router.get("/google/login")
async def google_login():
    """Redirect to Google OAuth."""
    from app.config import get_settings
    s = get_settings()
    url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={s.GOOGLE_CLIENT_ID}&redirect_uri={s.GOOGLE_REDIRECT_URI}"
        f"&response_type=code&scope=openid+email+profile&access_type=offline"
    )
    return {"url": url}


@router.get("/google/callback")
async def google_callback(code: str, db: AsyncSession = Depends(get_db)):
    """Handle Google OAuth callback."""
    from app.integrations.google_oauth import exchange_google_code, get_google_user
    token_data = await exchange_google_code(code)
    google_user = await get_google_user(token_data["access_token"])

    result = await db.execute(
        select(OAuthAccount).where(OAuthAccount.provider == "google", OAuthAccount.provider_user_id == google_user["sub"])
    )
    oauth = result.scalar_one_or_none()

    if oauth:
        user_result = await db.execute(select(User).where(User.id == oauth.user_id))
        user = user_result.scalar_one()
    else:
        username = google_user["email"].split("@")[0]
        user = User(
            email=google_user["email"],
            username=username,
            full_name=google_user.get("name", username),
            avatar=google_user.get("picture"),
            role=UserRole.DEVELOPER,
            status=UserStatus.ACTIVE,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        db.add(DeveloperProfile(user_id=user.id))
        db.add(OAuthAccount(
            user_id=user.id,
            provider="google",
            provider_user_id=google_user["sub"],
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
        ))
        await db.flush()

    access_token = create_access_token(user.id, user.role.value)
    refresh_token = create_refresh_token(user.id)
    db.add(Session(user_id=user.id, refresh_token=refresh_token, expires_at="", is_active=True))

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user_id=str(user.id),
        role=user.role.value,
    )
