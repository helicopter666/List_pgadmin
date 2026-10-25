from functools import lru_cache
from sqlalchemy import create_engine, text
from .config import settings

@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True, hide_parameters=True)

def rows(sql, params=None):
    with engine().connect() as c:
        return [dict(r) for r in c.execute(text(sql), params or {}).mappings()]

def row(sql, params=None):
    data = rows(sql, params)
    return data[0] if data else None
