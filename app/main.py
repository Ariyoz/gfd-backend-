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
from app.middleware.security import SecurityHeadersMiddleware, RequestIDMiddleware, RequestLoggingMiddleware

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
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

    # Broadcast online status
    from app.websocket.events import broadcast_event, EventType
    await broadcast_event(EventType.USER_ONLINE, {"user_id": user_id})

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

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, user_id)
        await broadcast_event(EventType.USER_OFFLINE, {"user_id": user_id})


# ── Health Check ──
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": settings.APP_VERSION}


@app.get("/")
async def root():
    return {"message": "GFD API", "version": settings.APP_VERSION, "docs": "/docs"}
