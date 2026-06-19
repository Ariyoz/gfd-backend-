"""GFD Backend — Main Application Entry Point."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.api.v1 import api_router
from app.websocket import ws_manager
from app.core.security import decode_token

settings = get_settings()

# Rate limiter
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    print(f"🚀 GFD Backend v{settings.APP_VERSION} starting...")

    # Auto-migrate: add missing columns
    try:
        from app.database.session import engine
        from sqlalchemy import text
        async with engine.begin() as conn:
            # Add view_count and like_count to projects if not exists
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='projects' AND column_name='view_count') THEN
                        ALTER TABLE projects ADD COLUMN view_count INTEGER DEFAULT 0;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='projects' AND column_name='like_count') THEN
                        ALTER TABLE projects ADD COLUMN like_count INTEGER DEFAULT 0;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='projects' AND column_name='cover_image') THEN
                        ALTER TABLE projects ADD COLUMN cover_image TEXT;
                    END IF;
                END $$;
            """))
            # Create project_likes and project_views tables if not exist
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS project_likes (
                    id SERIAL PRIMARY KEY,
                    project_id UUID NOT NULL,
                    user_id UUID NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(project_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS project_views (
                    id SERIAL PRIMARY KEY,
                    project_id UUID NOT NULL,
                    user_id UUID NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(project_id, user_id)
                );
            """))
            # Create jobs tables
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    poster_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title VARCHAR(300) NOT NULL,
                    company VARCHAR(200),
                    company_logo TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    requirements TEXT,
                    responsibilities TEXT,
                    skills_required TEXT[] DEFAULT '{}',
                    job_type VARCHAR(20) DEFAULT 'full_time',
                    experience_level VARCHAR(50),
                    location VARCHAR(200),
                    is_remote BOOLEAN DEFAULT TRUE,
                    salary_min FLOAT,
                    salary_max FLOAT,
                    salary_currency VARCHAR(10) DEFAULT 'USD',
                    status VARCHAR(20) DEFAULT 'open',
                    application_count INTEGER DEFAULT 0,
                    view_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS job_applications (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    applicant_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    cover_letter TEXT,
                    resume_url TEXT,
                    portfolio_url TEXT,
                    linkedin_url TEXT,
                    github_url TEXT,
                    years_experience INTEGER,
                    expected_salary FLOAT,
                    availability VARCHAR(100),
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(job_id, applicant_id)
                );
            """))
            # Create subscriptions table
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    plan VARCHAR(50) NOT NULL DEFAULT 'free',
                    billing_cycle VARCHAR(20) DEFAULT 'monthly',
                    status VARCHAR(20) DEFAULT 'active',
                    payment_reference TEXT,
                    started_at TIMESTAMP DEFAULT NOW(),
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """))
            # ── Phase 2: add reactions column to messages (safe) ──
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='messages' AND column_name='reactions') THEN
                        ALTER TABLE messages ADD COLUMN reactions JSONB DEFAULT '{}';
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='projects' AND column_name='repository_url') THEN
                        ALTER TABLE projects ADD COLUMN repository_url TEXT;
                    END IF;
                END $$;
            """))
        print("✅ Database columns verified")
    except Exception as e:
        print(f"⚠️ Migration check: {e}")

    yield
    # Shutdown
    print("👋 GFD Backend shutting down...")


app = FastAPI(
    title="GFD API",
    description="Global Fullstack Developers — Backend API",
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Middleware ──
from app.middleware.security import SecurityHeadersMiddleware, RequestIDMiddleware, RequestLoggingMiddleware, InputSanitizationMiddleware

app.add_middleware(InputSanitizationMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins + ["https://globalfd.xyz", "https://www.globalfd.xyz"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── API Routes ──
app.include_router(api_router)


# ── WebSocket Endpoint ──
@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    """WebSocket connection for real-time features."""
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=4001)
        return

    user_id = payload.get("sub")
    await ws_manager.connect(websocket, user_id)

    # Update DB online status
    try:
        from app.database.session import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as session:
            await session.execute(text("UPDATE users SET is_online = TRUE WHERE id = CAST(:uid AS UUID)"), {"uid": user_id})
            await session.commit()
    except Exception as e:
        print(f"[WARN] Failed to set online: {e}")

    # Broadcast online status to all other users
    from app.websocket.events import broadcast_event, EventType
    await broadcast_event(EventType.USER_ONLINE, {"user_id": user_id})

    # Send list of currently online users to the newly connected user
    online_users = [uid for uid in ws_manager.active_connections.keys() if uid != user_id]
    if online_users:
        await ws_manager.send_to_user(user_id, {
            "type": "online_users",
            "data": {"user_ids": online_users},
        })

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "typing_start":
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "typing_start",
                    "from": user_id,
                    "conversation_id": data.get("conversation_id"),
                })

            elif msg_type == "typing_stop":
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "typing_stop",
                    "from": user_id,
                    "conversation_id": data.get("conversation_id"),
                })

            elif msg_type == "message":
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "message_sent",
                    "from": user_id,
                    "from_name": data.get("from_name", ""),
                    "from_avatar": data.get("from_avatar", ""),
                    "content": data.get("content"),
                    "conversation_id": data.get("conversation_id"),
                    "timestamp": data.get("timestamp"),
                })

            elif msg_type == "message_read":
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "message_read",
                    "from": user_id,
                    "conversation_id": data.get("conversation_id"),
                    "message_id": data.get("message_id"),
                })

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            # ── Call Signaling ──
            elif msg_type == "call_initiate":
                target = data.get("to")
                print(f"[CALL] {user_id} calling {target} | Online: {ws_manager.is_online(target) if target else False}")
                if target:
                    await ws_manager.send_to_user(target, {
                        "type": "incoming_call",
                        "from": user_id,
                        "call_type": data.get("call_type", "voice"),
                        "caller_name": data.get("caller_name", ""),
                        "caller_avatar": data.get("caller_avatar", ""),
                        "offer": data.get("offer"),
                    })

            elif msg_type == "call_accept":
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "call_accepted",
                    "from": user_id,
                    "answer": data.get("answer"),
                })

            elif msg_type == "call_reject":
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "call_rejected",
                    "from": user_id,
                })

            elif msg_type == "call_end":
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "call_ended",
                    "from": user_id,
                })

            elif msg_type == "webrtc_ice":
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "webrtc_ice",
                    "from": user_id,
                    "candidate": data.get("candidate"),
                })

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, user_id)
        # Update DB offline status
        try:
            from app.database.session import AsyncSessionLocal
            from sqlalchemy import text
            async with AsyncSessionLocal() as session:
                await session.execute(text("UPDATE users SET is_online = FALSE WHERE id = CAST(:uid AS UUID)"), {"uid": user_id})
                await session.commit()
        except Exception as e:
            print(f"[WARN] Failed to set offline: {e}")
            pass
        await broadcast_event(EventType.USER_OFFLINE, {"user_id": user_id})


# ── Health Check ──
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": settings.APP_VERSION}


@app.get("/")
async def root():
    return {"message": "GFD API", "version": settings.APP_VERSION, "docs": "/docs"}
