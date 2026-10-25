import pytest
from sqlalchemy import text
from app.security import verify_password
from test_application import csrf, login


def payload(token, **changes):
    return {'csrf': token, 'login': 'new.user', 'display_name': 'Новый пользователь',
            'password': 'New-user-password-123', 'password_confirm': 'New-user-password-123', **changes}


def test_registration_reader_isolation_and_login(client, db):
    page = client.get('/register')
    assert page.status_code == 200 and 'Повторите пароль' in page.text
    assert '/register' in client.get('/login').text
    old_csrf = csrf(page)
    r = client.post('/register', data=payload(old_csrf, login='  NEW.User  ', role='admin'), follow_redirects=False)
    assert r.status_code == 303
    home = client.get('/')
    assert home.status_code == 200 and 'Новый пользователь' in home.text
    assert csrf(home) != old_csrf
    assert 'Доступная коллекция' not in home.text
    assert client.get('/admin').status_code == 403
    assert client.get('/upload').status_code == 403
    with db.connect() as c:
        account = c.execute(text('''SELECT u.*, r.code FROM docsearch.users u
            JOIN docsearch.roles r USING(role_id) WHERE login='new.user' ''')).mappings().one()
        assert account['code'] == 'reader' and account['is_active']
        assert verify_password(account['password_hash'], 'New-user-password-123')
        assert c.execute(text('SELECT count(*) FROM docsearch.collection_access WHERE user_id=:id'),
                         {'id': account['user_id']}).scalar_one() == 0
    client.post('/logout', data={'csrf': csrf(home)})
    r = client.post('/login', data={'csrf':csrf(client.get('/login')), 'login':'NEW.USER',
                                  'password':'New-user-password-123'}, follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.parametrize('changes', [
    {'password_confirm': 'Different-password'},
    {'password': 'short', 'password_confirm': 'short'},
    {'display_name': '   '}, {'login': 'ab'}, {'login': 'bad login'}, {'login': '<script>'},
])
def test_registration_validation(client, db, changes):
    r = client.post('/register', data=payload(csrf(client.get('/register')), **changes))
    assert r.status_code == 400
    assert 'value="New-user-password-123"' not in r.text
    with db.connect() as c:
        assert c.execute(text('SELECT count(*) FROM docsearch.users')).scalar_one() == 3


def test_registration_duplicate_does_not_replace_account(client, db):
    r = client.post('/register', data=payload(csrf(client.get('/register')), login='ADMIN'))
    assert r.status_code == 409 and 'уже занят' in r.text
    login(client)  # старый пароль администратора по-прежнему работает


def test_registration_csrf_and_authenticated_redirect(client):
    assert client.post('/register', data=payload('invalid')).status_code == 403
    token = login(client)
    for url in ['/register', '/login']:
        assert client.get(url, follow_redirects=False).status_code == 303
    assert client.post('/register', data=payload(token), follow_redirects=False).status_code == 303


def test_registration_rate_limit(client):
    token = csrf(client.get('/register'))
    for _ in range(15):
        assert client.post('/register', data=payload(token, login='ab')).status_code == 400
    assert client.post('/register', data=payload(token)).status_code == 429
    # Регистрация не исчерпывает отдельный лимит входа.
    login(client)
