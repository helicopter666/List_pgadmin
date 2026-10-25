"""Согласованная схема из 10 таблиц; допускает принятие проверенной существующей БД."""
import json
from pathlib import Path
from alembic import op
from sqlalchemy import text
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None
ROOT = Path(__file__).resolve().parents[2]

def snapshot(c):
    columns = c.execute(text('''SELECT c.relname AS table_name,a.attname AS column_name,
       pg_catalog.format_type(a.atttypid,a.atttypmod) AS data_type,a.attnotnull AS not_null,
       a.attidentity AS identity
       FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
       JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname='docsearch' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
       ORDER BY c.relname,a.attnum''')).mappings().all()
    constraints = c.execute(text('''SELECT c.relname AS table_name,con.conname,
       pg_get_constraintdef(con.oid) AS definition
       FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
       JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='docsearch' AND con.contype IN ('p','u','f','c')
       ORDER BY c.relname,con.conname''')).mappings().all()
    return {'columns':[dict(x) for x in columns], 'constraints':[dict(x) for x in constraints]}

def upgrade():
    c = op.get_bind()
    existing = c.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='docsearch'")).scalar_one()
    if existing:
        expected = json.loads((ROOT/'sql'/'expected_schema.json').read_text())
        actual = snapshot(c)
        if actual != expected:
            raise RuntimeError('Существующая схема docsearch отличается от согласованной. Данные не изменены. Используйте отдельную БД или подготовьте миграцию различий. Не выполняйте alembic stamp вслепую.')
    sql = (ROOT/'sql'/'initial.sql').read_text(encoding='utf-8')
    # Без внешних BEGIN/COMMIT: транзакцией управляет Alembic.
    c.exec_driver_sql(sql, execution_options={"no_parameters": True})

def downgrade():
    raise RuntimeError('Автоматическое удаление базы с документами отключено. Восстановите резервную копию для отката.')
