# GFD Backend — Global Fullstack Developers

Production-ready FastAPI backend for the GFD platform.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | FastAPI (async) |
| Database | Supabase PostgreSQL |
| ORM | SQLAlchemy (async) + Alembic |
| Auth | JWT + OAuth (GitHub, Google) |
| Cache | Redis |
| Background Tasks | Celery + Redis |
| File Storage | Cloudinary |
| Email | Resend |
| Real-time | WebSockets |
| Deployment | Docker + Render |

## Quick Start

```bash
# 1. Clone and setup
cd gfd-backend
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Fill in your credentials

# 3. Run migrations
alembic upgrade head

# 4. Start server
uvicorn app.main:app --reload --port 8000
```

## API Documentation

Once running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Project Structure

```
app/
├── api/v1/endpoints/    # Route handlers
│   ├── auth.py          # Register, login, OAuth, tokens
│   ├── users.py         # Profiles, follow/unfollow
│   ├── feed.py          # Posts, likes, comments, bookmarks
│   ├── projects.py      # Hiring, applications
│   ├── messages.py      # Conversations, messaging
│   ├── notifications.py # Notification CRUD
│   ├── github.py        # GitHub sync
│   ├── uploads.py       # File uploads (Cloudinary)
│   └── admin.py         # Admin moderation
├── auth/                # Auth utilities
├── config/              # Settings (pydantic-settings)
├── core/                # Security, dependencies, RBAC
├── database/            # SQLAlchemy session, Base
├── integrations/        # GitHub OAuth, Google OAuth, Cloudinary, Resend
├── middleware/          # Security headers, logging, request ID
├── models/              # SQLAlchemy models (20+ tables)
├── schemas/             # Pydantic request/response schemas
├── services/            # Business logic (feed, cache, notifications)
├── tasks/               # Celery background tasks
├── tests/               # pytest async tests
├── utils/               # Helpers
├── websocket/           # WebSocket connection manager
└── main.py              # FastAPI app entry point
```

## Database Tables

- `users` — Core user accounts
- `developer_profiles` — Developer-specific data
- `client_profiles` — Client/company data
- `oauth_accounts` — GitHub/Google OAuth tokens
- `sessions` — Refresh token sessions
- `github_profiles` — Synced GitHub data
- `repositories` — Synced GitHub repos
- `posts` — Social feed posts
- `comments` — Post comments (nested)
- `likes` — Post likes
- `bookmarks` — Saved posts
- `follows` — Follow relationships
- `hashtags` — Trending hashtags
- `blocked_users` — Block list
- `reports` — Content reports
- `projects` — Client job postings
- `applications` — Developer applications
- `conversations` — Chat conversations
- `conversation_participants` — Chat members
- `messages` — Chat messages
- `notifications` — User notifications
- `activity_logs` — User activity tracking
- `audit_logs` — Admin action audit trail

## Authentication Flow

1. **Register** → `POST /api/v1/auth/register` → returns JWT tokens
2. **Login** → `POST /api/v1/auth/login` → returns JWT tokens
3. **GitHub OAuth** → `GET /api/v1/auth/github/login` → redirect → callback → tokens
4. **Google OAuth** → `GET /api/v1/auth/google/login` → redirect → callback → tokens
5. **Refresh** → `POST /api/v1/auth/refresh` → rotates tokens
6. **Logout** → `POST /api/v1/auth/logout` → revokes refresh token

## Docker Deployment

```bash
docker-compose up --build
```

Services:
- `api` — FastAPI on port 8000
- `celery-worker` — Background task processing
- `celery-beat` — Scheduled tasks
- `redis` — Cache + message broker

## Running Tests

```bash
pytest app/tests/ -v
```

## Environment Variables

See `.env.example` for all required configuration.
