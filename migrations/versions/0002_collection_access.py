"""Коллекции для всех зарегистрированных; существующие коллекции остаются закрытыми."""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('collections', sa.Column('access_level', sa.String(20),
                  nullable=False, server_default='private'), schema='docsearch')
    op.create_check_constraint('ck_collections_access_level', 'collections',
                               "access_level IN ('private', 'registered')", schema='docsearch')


def downgrade():
    raise RuntimeError('Откат настроек доступа выполняется по резервной копии; автоматическое удаление отключено.')
