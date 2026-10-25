import os
from pathlib import Path
import pytest
os.environ.setdefault('SESSION_SECRET','tests-only-secret-not-for-a-running-server-1234567890')
if os.environ.get('TEST_DATABASE_URL'):
    os.environ['DATABASE_URL'] = os.environ['TEST_DATABASE_URL']

@pytest.fixture
def db(tmp_path):
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Для интеграционных тестов задайте TEST_DATABASE_URL отдельной тестовой БД.')
    if os.environ.get('RESET_TEST_DATABASE') != '1':
        pytest.fail('Тесты очищают БД. Подтвердите отдельную тестовую БД: RESET_TEST_DATABASE=1.')
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from app.db import engine
    from app.config import settings
    command.upgrade(Config('alembic.ini'),'head')
    with engine().begin() as c:
        c.execute(text('''TRUNCATE docsearch.users,docsearch.collections,docsearch.categories,
            docsearch.embedding_models RESTART IDENTITY CASCADE'''))
    original = settings().storage_dir
    settings().storage_dir = tmp_path/'uploads'
    yield engine()
    settings().storage_dir = original

@pytest.fixture
def accounts(db):
    from sqlalchemy import text
    from app.security import password_hash
    encoded = password_hash('Testing-password-123')
    with db.begin() as c:
        for login,role in [('admin','admin'),('editor','editor'),('reader','reader')]:
            c.execute(text('''INSERT INTO docsearch.users(login,password_hash,display_name,role_id)
                SELECT :login,:hash,:login,role_id FROM docsearch.roles WHERE code=:role'''),
                {'login':login,'hash':encoded,'role':role})
        for name in ['Доступная коллекция','Скрытая коллекция']:
            c.execute(text('INSERT INTO docsearch.collections(name) VALUES (:n)'),{'n':name})
        c.execute(text('INSERT INTO docsearch.collection_access VALUES (2,1),(3,1)'))
        c.execute(text("INSERT INTO docsearch.categories(name) VALUES ('Обучение')"))
    return {'password':'Testing-password-123'}

@pytest.fixture
def client(accounts):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import _attempts
    _attempts.clear()
    with TestClient(app) as client:
        yield client

class FakeEncoder:
    """Только тестовая подмена. В рабочем приложении такого режима нет."""
    profile='pytest-only-profile'
    model_name='pytest-only'
    revision='pytest-fixed'
    dimension=3
    max_tokens=512
    config={'fixture':True}
    def token_count(self,t):return max(1,len(t)//4)
    def query(self,t):
        t=t.lower()
        return [1,0.1,0.1] if any(x in t for x in ['баз','резерв','postgres','копи']) else [0.1,1,0.1]
    def passages(self,values):return [self.query(t) for t in values]

@pytest.fixture
def encoder():return FakeEncoder()
