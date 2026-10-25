from alembic import context
from app.db import engine

if context.is_offline_mode():
    raise RuntimeError('Для проверки существующей схемы миграции выполняются с подключением к БД.')
with engine().connect() as connection:
    context.configure(connection=connection, target_metadata=None, transaction_per_migration=True, version_table_schema="public")
    with context.begin_transaction():
        context.run_migrations()
