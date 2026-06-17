"""Phase 2: Messaging, Feed & Hiring upgrade — add new columns

Revision ID: phase2_upgrade
Revises: add_project_views_likes
Create Date: 2026-06-17

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'phase2_upgrade'
down_revision = 'add_project_views_likes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── messages table ──────────────────────────────────────────────────────
    op.add_column('messages', sa.Column('reactions', postgresql.JSONB(astext_type=sa.Text()), nullable=True, server_default='{}'))
    op.add_column('messages', sa.Column('link_preview', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('messages', sa.Column('status', sa.String(20), nullable=True, server_default='sent'))
    op.add_column('messages', sa.Column('file_type', sa.String(50), nullable=True))

    # ── conversations table ──────────────────────────────────────────────────
    op.add_column('conversations', sa.Column('job_id', postgresql.UUID(as_uuid=True), nullable=True))

    # Add 'hiring' to conversation type enum (safe — only adds new value)
    op.execute("ALTER TYPE conversationtype ADD VALUE IF NOT EXISTS 'hiring'")

    # ── conversation_participants table ──────────────────────────────────────
    # unread_count may already exist from inline migration — use IF NOT EXISTS pattern
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='conversation_participants' AND column_name='unread_count'
            ) THEN
                ALTER TABLE conversation_participants ADD COLUMN unread_count INTEGER DEFAULT 0;
            END IF;
        END $$;
    """)

    # ── posts table ──────────────────────────────────────────────────────────
    op.add_column('posts', sa.Column('video_url', sa.Text(), nullable=True))
    op.add_column('posts', sa.Column('document_url', sa.Text(), nullable=True))
    op.add_column('posts', sa.Column('document_name', sa.String(255), nullable=True))
    op.add_column('posts', sa.Column('link_preview', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    # Add 'link' and 'document' to posttype enum
    op.execute("ALTER TYPE posttype ADD VALUE IF NOT EXISTS 'link'")
    op.execute("ALTER TYPE posttype ADD VALUE IF NOT EXISTS 'document'")

    # ── notifications table — add job_invitation to notificationtype enum ──
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'job_invitation'")

    # ── job_applications table ──────────────────────────────────────────────
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='job_applications' AND column_name='expected_salary_currency'
            ) THEN
                ALTER TABLE job_applications ADD COLUMN expected_salary_currency VARCHAR(10) DEFAULT 'USD';
            END IF;
        END $$;
    """)

    # ── indexes for performance ──────────────────────────────────────────────
    op.create_index(
        'ix_messages_status',
        'messages',
        ['status'],
        unique=False,
    )
    op.create_index(
        'ix_posts_created_at',
        'posts',
        ['created_at'],
        unique=False,
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index('ix_posts_created_at', table_name='posts')
    op.drop_index('ix_messages_status', table_name='messages')

    # Drop columns (enums can't be easily removed — leave them)
    op.drop_column('job_applications', 'expected_salary_currency')
    op.drop_column('posts', 'link_preview')
    op.drop_column('posts', 'document_name')
    op.drop_column('posts', 'document_url')
    op.drop_column('posts', 'video_url')
    op.drop_column('conversations', 'job_id')
    op.drop_column('messages', 'file_type')
    op.drop_column('messages', 'status')
    op.drop_column('messages', 'link_preview')
    op.drop_column('messages', 'reactions')
