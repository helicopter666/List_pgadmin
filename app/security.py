import secrets
import time
from collections import defaultdict
from threading import Lock
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException
from .db import row

hasher = PasswordHasher()
_dummy_hash = hasher.hash(secrets.token_urlsafe(24))
_attempts = defaultdict(list)
_lock = Lock()

def password_hash(password):
    if not 10 <= len(password) <= 256:
        raise ValueError('Пароль должен содержать от 10 до 256 символов.')
    return hasher.hash(password)

def verify_password(encoded, password):
    try:
        return hasher.verify(encoded or _dummy_hash, password)
    except (VerificationError, InvalidHashError):
        return False

def csrf_token(request):
    if 'csrf' not in request.session:
        request.session['csrf'] = secrets.token_hex(32)
    return request.session['csrf']

def check_csrf(request, token):
    expected = request.session.get('csrf', '')
    if not token or not expected or not secrets.compare_digest(str(token), expected):
        raise HTTPException(403, 'Форма устарела. Обновите страницу и повторите действие.')

def current_user(request):
    uid = request.session.get('user_id')
    if not isinstance(uid, int):
        return None
    return row('''SELECT u.user_id, u.login, u.display_name, r.code AS role
                  FROM docsearch.users u JOIN docsearch.roles r USING(role_id)
                  WHERE u.user_id=:id AND u.is_active''', {'id': uid})

def require_user(request):
    user = current_user(request)
    if not user:
        raise HTTPException(303, headers={'Location': '/login'})
    return user

def require_admin(user):
    if user['role'] != 'admin':
        raise HTTPException(403, 'Действие доступно администратору.')

def require_editor(user):
    if user['role'] not in ('editor', 'admin'):
        raise HTTPException(403, 'Для изменения документов нужна роль редактора.')

def collection_allowed(user, collection_id):
    return user['role'] == 'admin' or bool(row(
        '''SELECT 1 AS ok FROM docsearch.collections c WHERE c.collection_id=:cid
           AND (c.access_level='registered' OR EXISTS (
             SELECT 1 FROM docsearch.collection_access ac
             WHERE ac.user_id=:uid AND ac.collection_id=c.collection_id))''',
        {'uid': user['user_id'], 'cid': collection_id}))

def check_collection(user, collection_id):
    if not collection_allowed(user, collection_id):
        raise HTTPException(404, 'Коллекция не найдена или недоступна.')

def login_limit(request, purpose='login'):
    # Один web-процесс в первой версии. Для нескольких процессов нужен общий лимитер.
    key = purpose + ':' + (request.client.host if request.client else 'unknown')
    now = time.monotonic()
    with _lock:
        if len(_attempts) > 10000:
            for k in list(_attempts):
                if not _attempts[k] or _attempts[k][-1] < now - 300:
                    del _attempts[k]
        times = [x for x in _attempts[key] if x > now - 300]
        if len(times) >= 15:
            raise HTTPException(429, 'Слишком много попыток. Повторите через пять минут.')
        _attempts[key] = times + [now]
