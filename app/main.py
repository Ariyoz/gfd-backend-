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

    try:
        while True:
            data = await websocket.receive_json()
            # Handle different message types
            msg_type = data.get("type")

            if msg_type == "typing":
                # Broadcast typing indicator to conversation participants
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "typing",
                    "from": user_id,
                    "conversation_id": data.get("conversation_id"),
                })

            elif msg_type == "message":
                # Real-time message delivery
                await ws_manager.send_to_user(data.get("to"), {
                    "type": "new_message",
                    "from": user_id,
                    "content": data.get("content"),
                    "conversation_id": data.get("conversation_id"),
                })

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, user_id)


# ── Health Check ──
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": settings.APP_VERSION}


@app.get("/")
async def root():
    return {"message": "GFD API", "version": settings.APP_VERSION, "docs": "/docs"}
