"""Add view_count and like_count to projects

Revision ID: add_views_likes
Revises: 3265ab5cd6be
Create Date: 2026-05-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'add_views_likes'
down_revision = '3265ab5cd6be'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('view_count', sa.Integer(), server_default='0'))
    op.add_column('projects', sa.Column('like_count', sa.Integer(), server_default='0'))


def downgrade() -> None:
    op.drop_column('projects', 'view_count')
    op.drop_column('projects', 'like_count')
