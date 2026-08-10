"""add crypto wallet tables

Revision ID: add_crypto_wallet_001
Revises: add_project_views_likes
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa

revision = 'add_crypto_wallet_001'
down_revision = 'add_project_views_likes'
branch_labels = None
depends_on = None


def upgrade():
    # Crypto wallets — one per user
    op.execute("""
        CREATE TABLE IF NOT EXISTS crypto_wallets (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at  TIMESTAMP DEFAULT NOW(),
            updated_at  TIMESTAMP DEFAULT NOW()
        )
    """)

    # Crypto transactions — deposits and withdrawals
    op.execute("""
        CREATE TABLE IF NOT EXISTS crypto_transactions (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            coin              VARCHAR(20) NOT NULL,
            type              VARCHAR(20) NOT NULL DEFAULT 'deposit',
            amount_credited   NUMERIC(36, 18) DEFAULT 0,
            amount_withdrawn  NUMERIC(36, 18) DEFAULT 0,
            usd_value         NUMERIC(20, 6) DEFAULT 0,
            tx_hash           VARCHAR(200),
            payment_id        VARCHAR(200) UNIQUE,
            from_address      VARCHAR(200),
            to_address        VARCHAR(200),
            network           VARCHAR(50),
            status            VARCHAR(30) NOT NULL DEFAULT 'pending',
            confirmations     INTEGER DEFAULT 0,
            created_at        TIMESTAMP DEFAULT NOW(),
            updated_at        TIMESTAMP DEFAULT NOW()
        )
    """)

    # Indexes for fast lookups
    op.execute("CREATE INDEX IF NOT EXISTS idx_crypto_tx_user ON crypto_transactions(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crypto_tx_coin ON crypto_transactions(coin)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crypto_tx_status ON crypto_transactions(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crypto_tx_payment_id ON crypto_transactions(payment_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS crypto_transactions")
    op.execute("DROP TABLE IF EXISTS crypto_wallets")
