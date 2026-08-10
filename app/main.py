"""GFD Backend — Main Application Entry Point."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
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
            # ── Wallet tables — ensure correct schema ──
            # Step 1: Create tables if they don't exist
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS wallets (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    balance NUMERIC(12,2) DEFAULT 0,
                    total_earned NUMERIC(12,2) DEFAULT 0,
                    total_withdrawn NUMERIC(12,2) DEFAULT 0,
                    total_spent NUMERIC(12,2) DEFAULT 0,
                    is_frozen BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS wallet_transactions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    wallet_id UUID NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                    type VARCHAR(20) NOT NULL,
                    amount NUMERIC(12,2) NOT NULL,
                    fee NUMERIC(12,2) DEFAULT 0,
                    balance_before NUMERIC(12,2),
                    balance_after NUMERIC(12,2),
                    description TEXT,
                    reference VARCHAR(100) UNIQUE,
                    provider VARCHAR(30),
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS virtual_accounts (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    bank_name VARCHAR(100),
                    account_name VARCHAR(200),
                    account_number VARCHAR(20),
                    provider VARCHAR(50),
                    customer_code VARCHAR(100),
                    dva_id VARCHAR(100),
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS withdrawal_requests (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    wallet_id UUID NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                    amount NUMERIC(12,2) NOT NULL,
                    fee NUMERIC(12,2) DEFAULT 0,
                    net_amount NUMERIC(12,2) NOT NULL,
                    bank_name VARCHAR(100) NOT NULL,
                    account_name VARCHAR(200) NOT NULL,
                    account_number VARCHAR(20) NOT NULL,
                    bank_code VARCHAR(20),
                    status VARCHAR(20) DEFAULT 'pending',
                    reference VARCHAR(100) UNIQUE,
                    provider VARCHAR(30),
                    rejection_reason TEXT,
                    processed_by UUID REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_wt_wallet ON wallet_transactions(wallet_id);
                CREATE INDEX IF NOT EXISTS idx_wt_reference ON wallet_transactions(reference);
                CREATE INDEX IF NOT EXISTS idx_wt_status ON wallet_transactions(status);
                CREATE INDEX IF NOT EXISTS idx_va_user ON virtual_accounts(user_id);
                CREATE INDEX IF NOT EXISTS idx_wr_user ON withdrawal_requests(user_id);
                CREATE INDEX IF NOT EXISTS idx_wr_status ON withdrawal_requests(status);
            """))
            # money_requests table for send/request money feature
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS money_requests (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    payer_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    amount NUMERIC(12,2) NOT NULL,
                    note TEXT,
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_mr_payer ON money_requests(payer_id);
                CREATE INDEX IF NOT EXISTS idx_mr_requester ON money_requests(requester_id);
            """))
            # Step 2: Add missing columns using ADD COLUMN IF NOT EXISTS (PostgreSQL 9.6+)
            await conn.execute(text("""
                ALTER TABLE wallets ADD COLUMN IF NOT EXISTS total_spent NUMERIC(12,2) DEFAULT 0;
                ALTER TABLE wallets ADD COLUMN IF NOT EXISTS is_frozen BOOLEAN DEFAULT FALSE;
                ALTER TABLE wallets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS fee NUMERIC(12,2) DEFAULT 0;
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS balance_before NUMERIC(12,2);
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS balance_after NUMERIC(12,2);
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS provider VARCHAR(30);
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
                ALTER TABLE virtual_accounts ADD COLUMN IF NOT EXISTS dva_id VARCHAR(100);
                ALTER TABLE virtual_accounts ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                ALTER TABLE virtual_accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
            """))
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
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='projects' AND column_name='github_url') THEN
                        ALTER TABLE projects ADD COLUMN github_url TEXT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='projects' AND column_name='live_url') THEN
                        ALTER TABLE projects ADD COLUMN live_url TEXT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='projects' AND column_name='cover_image') THEN
                        ALTER TABLE projects ADD COLUMN cover_image TEXT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='projects' AND column_name='view_count') THEN
                        ALTER TABLE projects ADD COLUMN view_count INTEGER DEFAULT 0;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='projects' AND column_name='like_count') THEN
                        ALTER TABLE projects ADD COLUMN like_count INTEGER DEFAULT 0;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='messages' AND column_name='status') THEN
                        ALTER TABLE messages ADD COLUMN status VARCHAR(20) DEFAULT 'sent';
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='messages' AND column_name='is_edited') THEN
                        ALTER TABLE messages ADD COLUMN is_edited BOOLEAN DEFAULT FALSE;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='messages' AND column_name='is_deleted') THEN
                        ALTER TABLE messages ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='messages' AND column_name='reply_to_id') THEN
                        ALTER TABLE messages ADD COLUMN reply_to_id UUID;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='messages' AND column_name='media_url') THEN
                        ALTER TABLE messages ADD COLUMN media_url TEXT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='messages' AND column_name='file_name') THEN
                        ALTER TABLE messages ADD COLUMN file_name TEXT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='messages' AND column_name='file_size') THEN
                        ALTER TABLE messages ADD COLUMN file_size INTEGER;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='conversation_participants' AND column_name='unread_count') THEN
                        ALTER TABLE conversation_participants ADD COLUMN unread_count INTEGER DEFAULT 0;
                    END IF;
                END $$;
            """))
            # ── Performance indexes ──
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_jobs_poster ON jobs(poster_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_job_apps_applicant ON job_applications(applicant_id);
                CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
                CREATE INDEX IF NOT EXISTS idx_conv_participants_conv ON conversation_participants(conversation_id);
                CREATE INDEX IF NOT EXISTS idx_conv_participants_user ON conversation_participants(user_id);
            """))
        print("✅ Database ready")
    except Exception as e:
        print(f"⚠️ Migration check: {e}")

    # ── Extend notificationtype enum with new values ──
    try:
        from app.database.session import engine
        from sqlalchemy import text as _t
        async with engine.begin() as conn:
            for val in ('transfer_received', 'money_request', 'request_accepted',
                        'request_rejected', 'hire_request', 'job_invite'):
                await conn.execute(_t(
                    f"ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS '{val}'"
                ))
        print("✅ Notification enum extended")
    except Exception as e:
        print(f"⚠️ Notification enum migration: {e}")

    # ── Wallet tables — run in a completely separate block so they ALWAYS execute ──
    try:
        from app.database.session import engine
        from sqlalchemy import text as _text
        async with engine.begin() as conn:
            print("🔧 Creating wallet tables...")
            await conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS wallets (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    balance NUMERIC(12,2) DEFAULT 0 NOT NULL,
                    total_earned NUMERIC(12,2) DEFAULT 0 NOT NULL,
                    total_withdrawn NUMERIC(12,2) DEFAULT 0 NOT NULL,
                    total_spent NUMERIC(12,2) DEFAULT 0 NOT NULL,
                    is_frozen BOOLEAN DEFAULT FALSE NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """))
            await conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS wallet_transactions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    wallet_id UUID NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                    type VARCHAR(20) NOT NULL,
                    amount NUMERIC(12,2) NOT NULL,
                    fee NUMERIC(12,2) DEFAULT 0,
                    balance_before NUMERIC(12,2),
                    balance_after NUMERIC(12,2),
                    description TEXT,
                    reference VARCHAR(100) UNIQUE,
                    provider VARCHAR(30),
                    status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """))
            await conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS virtual_accounts (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    bank_name VARCHAR(100),
                    account_name VARCHAR(200),
                    account_number VARCHAR(20),
                    provider VARCHAR(50),
                    customer_code VARCHAR(100),
                    dva_id VARCHAR(100),
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """))
            await conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS withdrawal_requests (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    wallet_id UUID NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                    amount NUMERIC(12,2) NOT NULL,
                    fee NUMERIC(12,2) DEFAULT 0,
                    net_amount NUMERIC(12,2) NOT NULL,
                    bank_name VARCHAR(100) NOT NULL,
                    account_name VARCHAR(200) NOT NULL,
                    account_number VARCHAR(20) NOT NULL,
                    bank_code VARCHAR(20),
                    status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                    reference VARCHAR(100) UNIQUE,
                    provider VARCHAR(30),
                    rejection_reason TEXT,
                    processed_by UUID REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """))
            await conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS money_requests (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    payer_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    amount NUMERIC(12,2) NOT NULL,
                    note TEXT,
                    status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """))
            # Indexes
            await conn.execute(_text("""
                CREATE INDEX IF NOT EXISTS idx_wt_wallet    ON wallet_transactions(wallet_id);
                CREATE INDEX IF NOT EXISTS idx_wt_reference ON wallet_transactions(reference);
                CREATE INDEX IF NOT EXISTS idx_wt_status    ON wallet_transactions(status);
                CREATE INDEX IF NOT EXISTS idx_va_user      ON virtual_accounts(user_id);
                CREATE INDEX IF NOT EXISTS idx_wr_user      ON withdrawal_requests(user_id);
                CREATE INDEX IF NOT EXISTS idx_wr_status    ON withdrawal_requests(status);
                CREATE INDEX IF NOT EXISTS idx_mr_payer     ON money_requests(payer_id);
                CREATE INDEX IF NOT EXISTS idx_mr_requester ON money_requests(requester_id);
            """))
            # Backfill any missing columns on existing tables
            await conn.execute(_text("""
                ALTER TABLE wallets ADD COLUMN IF NOT EXISTS total_spent NUMERIC(12,2) DEFAULT 0;
                ALTER TABLE wallets ADD COLUMN IF NOT EXISTS is_frozen BOOLEAN DEFAULT FALSE;
                ALTER TABLE wallets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS fee NUMERIC(12,2) DEFAULT 0;
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS balance_before NUMERIC(12,2);
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS balance_after NUMERIC(12,2);
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS provider VARCHAR(30);
                ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
                ALTER TABLE virtual_accounts ADD COLUMN IF NOT EXISTS dva_id VARCHAR(100);
                ALTER TABLE virtual_accounts ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                ALTER TABLE virtual_accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
                ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS processed_by UUID REFERENCES users(id) ON DELETE SET NULL;
                ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
            """))
        print("✅ Wallet tables ready")
    except Exception as e:
        print(f"❌ Wallet table migration failed: {e}")

    # ── Crypto wallet tables ──────────────────────────────────────────────────
    try:
        from app.database.session import engine
        from sqlalchemy import text as _ct
        async with engine.begin() as conn:
            await conn.execute(_ct("""
                CREATE TABLE IF NOT EXISTS crypto_wallets (
                    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id    UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS crypto_transactions (
                    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    coin             VARCHAR(20) NOT NULL,
                    type             VARCHAR(20) NOT NULL DEFAULT 'deposit',
                    amount_credited  NUMERIC(36,18) DEFAULT 0,
                    amount_withdrawn NUMERIC(36,18) DEFAULT 0,
                    usd_value        NUMERIC(20,6)  DEFAULT 0,
                    tx_hash          VARCHAR(200),
                    payment_id       VARCHAR(200) UNIQUE,
                    from_address     VARCHAR(200),
                    to_address       VARCHAR(200),
                    network          VARCHAR(50),
                    status           VARCHAR(30) NOT NULL DEFAULT 'pending',
                    confirmations    INTEGER DEFAULT 0,
                    created_at       TIMESTAMP DEFAULT NOW(),
                    updated_at       TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_crypto_tx_user       ON crypto_transactions(user_id);
                CREATE INDEX IF NOT EXISTS idx_crypto_tx_coin       ON crypto_transactions(coin);
                CREATE INDEX IF NOT EXISTS idx_crypto_tx_status     ON crypto_transactions(status);
                CREATE INDEX IF NOT EXISTS idx_crypto_tx_payment_id ON crypto_transactions(payment_id);
            """))
        print("✅ Crypto tables ready")
    except Exception as e:
        print(f"❌ Crypto table migration failed: {e}")

    # ── PIN and KYC tables ────────────────────────────────────────────────────
    try:
        from app.database.session import engine
        from sqlalchemy import text as _pt
        async with engine.begin() as conn:
            await conn.execute(_pt("""
                CREATE TABLE IF NOT EXISTS wallet_pins (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id         UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    pin_hash        TEXT NOT NULL,
                    failed_attempts INTEGER DEFAULT 0,
                    locked_until    TIMESTAMP,
                    created_at      TIMESTAMP DEFAULT NOW(),
                    updated_at      TIMESTAMP DEFAULT NOW()
                )
            """))
            await conn.execute(_pt("CREATE INDEX IF NOT EXISTS idx_wp_user ON wallet_pins(user_id)"))
            await conn.execute(_pt("""
                CREATE TABLE IF NOT EXISTS kyc_submissions (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id         UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    full_name       TEXT NOT NULL,
                    date_of_birth   TEXT NOT NULL,
                    country         TEXT NOT NULL,
                    id_type         TEXT NOT NULL,
                    id_number       TEXT NOT NULL,
                    id_front_url    TEXT NOT NULL,
                    id_back_url     TEXT,
                    selfie_url      TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    level           INTEGER DEFAULT 1,
                    reject_reason   TEXT,
                    reviewed_by     UUID REFERENCES users(id) ON DELETE SET NULL,
                    reviewed_at     TIMESTAMP,
                    created_at      TIMESTAMP DEFAULT NOW(),
                    updated_at      TIMESTAMP DEFAULT NOW()
                )
            """))
            await conn.execute(_pt("CREATE INDEX IF NOT EXISTS idx_kyc_user   ON kyc_submissions(user_id)"))
            await conn.execute(_pt("CREATE INDEX IF NOT EXISTS idx_kyc_status ON kyc_submissions(status)"))
            await conn.execute(_pt("ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_level INTEGER DEFAULT 0"))
            await conn.execute(_pt("ALTER TABLE users ADD COLUMN IF NOT EXISTS transaction_pin_set BOOLEAN DEFAULT FALSE"))
        print("✅ PIN + KYC tables ready")
    except Exception as e:
        print(f"❌ PIN/KYC table migration failed: {e}")
        import traceback; traceback.print_exc()

    # ── Keep-alive: ping self every 10 min so Render free tier stays warm ──
    import asyncio
    import httpx

    async def _keep_alive():
        await asyncio.sleep(60)  # wait 1 min after boot before first ping
        while True:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get("https://gfd-backend.onrender.com/health")
                print("🏓 Keep-alive ping sent")
            except Exception:
                pass  # silently ignore — server may be restarting
            await asyncio.sleep(600)  # every 10 minutes

    keep_alive_task = asyncio.create_task(_keep_alive())

    yield

    # Shutdown
    keep_alive_task.cancel()
    print("👋 GFD Backend shutting down...")


app = FastAPI(
    title="GFD API",
    description="Global Fullstack Developers — Backend API",
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS must be added FIRST (outermost middleware — runs before everything else) ──
# This ensures preflight OPTIONS requests always get Access-Control-Allow-Origin headers
_CORS_ORIGINS = list(set(
    settings.cors_origins
    + ["https://globalfd.xyz", "https://www.globalfd.xyz",
       "http://localhost:5173", "http://localhost:3000"]
))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# ── Security + logging middleware (added AFTER CORS so they run inside CORS) ──
from app.middleware.security import (
    SecurityHeadersMiddleware,
    RequestIDMiddleware,
    RequestLoggingMiddleware,
    InputSanitizationMiddleware,
    BruteForceMiddleware,
)

app.add_middleware(InputSanitizationMiddleware)
app.add_middleware(BruteForceMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RequestLoggingMiddleware)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Global exception handler — always sends CORS headers even on 500 ──
from fastapi import Request
from fastapi.responses import JSONResponse

import traceback as _tb
import logging as _log
_err_log = _log.getLogger("gfd.errors")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    _err_log.error(f"UNHANDLED {request.method} {request.url.path}: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
    origin = request.headers.get("origin", "")
    cors_origin = origin if origin in _CORS_ORIGINS else (_CORS_ORIGINS[0] if _CORS_ORIGINS else "*")
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {str(exc)[:300]}"},
        headers={
            "Access-Control-Allow-Origin": cors_origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        },
    )

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


# ── Server-side diagnostic — checks DB schema + Paystack key ──
@app.get("/diag")
async def diag():
    from app.config import get_settings as gs
    from app.database.session import AsyncSessionLocal
    from sqlalchemy import text as t
    s = gs()

    result = {
        "paystack_key_set": bool(s.PAYSTACK_SECRET_KEY),
        "paystack_key_prefix": (s.PAYSTACK_SECRET_KEY[:10] + "...") if s.PAYSTACK_SECRET_KEY else "EMPTY",
        "frontend_url": s.FRONTEND_URL,
        "db_check": {},
        "insert_test": None,
        "create_attempt": None,
    }

    try:
        async with AsyncSessionLocal() as db:
            # Try to create tables directly if they don't exist
            try:
                await db.execute(t("""
                    CREATE TABLE IF NOT EXISTS wallets (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                        balance NUMERIC(12,2) DEFAULT 0 NOT NULL,
                        total_earned NUMERIC(12,2) DEFAULT 0 NOT NULL,
                        total_withdrawn NUMERIC(12,2) DEFAULT 0 NOT NULL,
                        total_spent NUMERIC(12,2) DEFAULT 0 NOT NULL,
                        is_frozen BOOLEAN DEFAULT FALSE NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                await db.execute(t("""
                    CREATE TABLE IF NOT EXISTS wallet_transactions (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        wallet_id UUID NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                        type VARCHAR(20) NOT NULL,
                        amount NUMERIC(12,2) NOT NULL,
                        fee NUMERIC(12,2) DEFAULT 0,
                        balance_before NUMERIC(12,2),
                        balance_after NUMERIC(12,2),
                        description TEXT,
                        reference VARCHAR(100) UNIQUE,
                        provider VARCHAR(30),
                        status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                await db.execute(t("""
                    CREATE TABLE IF NOT EXISTS virtual_accounts (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                        bank_name VARCHAR(100),
                        account_name VARCHAR(200),
                        account_number VARCHAR(20),
                        provider VARCHAR(50),
                        customer_code VARCHAR(100),
                        dva_id VARCHAR(100),
                        is_active BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                await db.execute(t("""
                    CREATE TABLE IF NOT EXISTS withdrawal_requests (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        wallet_id UUID NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                        amount NUMERIC(12,2) NOT NULL,
                        fee NUMERIC(12,2) DEFAULT 0,
                        net_amount NUMERIC(12,2) NOT NULL,
                        bank_name VARCHAR(100) NOT NULL,
                        account_name VARCHAR(200) NOT NULL,
                        account_number VARCHAR(20) NOT NULL,
                        bank_code VARCHAR(20),
                        status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                        reference VARCHAR(100) UNIQUE,
                        provider VARCHAR(30),
                        rejection_reason TEXT,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                await db.execute(t("""
                    CREATE TABLE IF NOT EXISTS money_requests (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        payer_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        amount NUMERIC(12,2) NOT NULL,
                        note TEXT,
                        status VARCHAR(20) DEFAULT 'pending' NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                await db.execute(t("CREATE INDEX IF NOT EXISTS idx_wt_wallet ON wallet_transactions(wallet_id)"))
                await db.execute(t("CREATE INDEX IF NOT EXISTS idx_wt_ref ON wallet_transactions(reference)"))
                await db.execute(t("CREATE INDEX IF NOT EXISTS idx_wt_status ON wallet_transactions(status)"))
                await db.execute(t("CREATE INDEX IF NOT EXISTS idx_va_user ON virtual_accounts(user_id)"))
                await db.execute(t("CREATE INDEX IF NOT EXISTS idx_wr_user ON withdrawal_requests(user_id)"))
                await db.commit()
                result["create_attempt"] = "OK — tables created/verified"
            except Exception as ce:
                await db.rollback()
                result["create_attempt"] = f"FAILED: {str(ce)}"

            # Check wallet_transactions columns
            r = await db.execute(t(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='wallet_transactions' ORDER BY ordinal_position"
            ))
            result["db_check"]["wallet_transactions_cols"] = [row[0] for row in r.fetchall()]

            r2 = await db.execute(t(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='wallets' ORDER BY ordinal_position"
            ))
            result["db_check"]["wallets_cols"] = [row[0] for row in r2.fetchall()]

            # Test INSERT
            try:
                await db.execute(t("""
                    INSERT INTO wallet_transactions
                      (id, wallet_id, type, amount, fee, description, reference, provider, status, created_at, updated_at)
                    VALUES
                      (gen_random_uuid(), gen_random_uuid(), 'credit', 1, 0,
                       'diag-test', 'DIAG-REF-001', 'paystack', 'pending', NOW(), NOW())
                """))
                await db.rollback()
                result["insert_test"] = "OK"
            except Exception as e:
                await db.rollback()
                result["insert_test"] = f"FAILED: {str(e)}"

    except Exception as e:
        result["db_check"]["error"] = str(e)

    return result


@app.get("/")
async def root():
    return {"message": "GFD API", "version": settings.APP_VERSION, "docs": "/docs"}

# Deploy trigger: 2026-06-23 11:18
