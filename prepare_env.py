"""Запускается Python 3 без зависимостей: создаёт локальный .env с новыми секретами."""
from pathlib import Path
import secrets
import os

root = Path(__file__).resolve().parent
path = root/'.env'
if path.exists():
    raise SystemExit('.env уже существует; файл не изменён.')
password = secrets.token_hex(24)
text = f'''# Только локальные настройки; не публикуйте этот файл.
SESSION_SECRET={secrets.token_hex(48)}
POSTGRES_PASSWORD={password}
DATABASE_URL=postgresql+psycopg://docsearch:{password}@localhost:5433/document_search
COOKIE_SECURE=false
MODEL_NAME=intfloat/multilingual-e5-small
MODEL_REVISION=614241f622f53c4eeff9890bdc4f31cfecc418b3
'''
fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
with os.fdopen(fd,'w') as out:
    out.write(text)
print('Создан .env. Далее: docker compose up --build -d')
