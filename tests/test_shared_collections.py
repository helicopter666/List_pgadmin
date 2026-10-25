from sqlalchemy import text
from alembic import command
from alembic.config import Config
from test_application import csrf, login, upload


def test_new_registration_sees_shared_documents_in_every_mode(client, db, encoder, monkeypatch):
    from app.worker import process_one
    from app import search as sm
    monkeypatch.setattr(sm, 'get_encoder', lambda: encoder)
    token = login(client)
    public_doc = upload(client, token, 1, title='ОБЩИЙ ДОКУМЕНТ')
    private_doc = upload(client, token, 2, title='ЗАКРЫТЫЙ ДОКУМЕНТ')
    while process_one(encoder):
        pass
    with db.begin() as c:
        c.execute(text("INSERT INTO docsearch.categories(name) VALUES ('Секретная категория')"))
        c.execute(text('INSERT INTO docsearch.document_categories VALUES (:id,1)'), {'id':public_doc})
        c.execute(text('INSERT INTO docsearch.document_categories VALUES (:id,2)'), {'id':private_doc})
    r = client.post('/admin/collections/1/access', data={'csrf':token,'access_level':'registered'}, follow_redirects=False)
    assert r.status_code == 303
    client.post('/logout', data={'csrf':token})
    token = csrf(client.get('/register'))
    r = client.post('/register', data={'csrf':token,'login':'fresh.reader','display_name':'Читатель',
                     'password':'Fresh-password-123','password_confirm':'Fresh-password-123'})
    assert r.status_code == 200
    home = client.get('/')
    assert 'ОБЩИЙ ДОКУМЕНТ' in home.text and 'ЗАКРЫТЫЙ ДОКУМЕНТ' not in home.text
    assert 'Обучение' in home.text and 'Секретная категория' not in home.text
    with db.connect() as c:
        assert c.execute(text('''SELECT count(*) FROM docsearch.collection_access ac JOIN docsearch.users u
                              USING(user_id) WHERE u.login='fresh.reader' ''')).scalar_one() == 0
    assert client.get(f'/documents/{public_doc}').status_code == 200
    assert client.get(f'/documents/{public_doc}/download').status_code == 200
    for url in [f'/documents/{private_doc}',f'/documents/{private_doc}/download','/?collection=2']:
        assert client.get(url).status_code == 404
    for mode in ['vector','lexical','hybrid']:
        r = client.get('/', params={'q':'резервная копия','mode':mode,'collection':1,'category':1})
        assert 'class="panel result"' in r.text and 'ОБЩИЙ ДОКУМЕНТ' in r.text
        assert 'ЗАКРЫТЫЙ ДОКУМЕНТ' not in r.text
    assert client.get('/upload').status_code == 403
    assert client.get('/admin').status_code == 403


def test_closing_collection_preserves_only_individual_access(client, db, encoder, monkeypatch):
    from app.worker import process_one
    from app import search as sm
    monkeypatch.setattr(sm, 'get_encoder', lambda: encoder)
    token = login(client)
    did = upload(client, token, 2, title='ОТКРЫВАЕМАЯ КОЛЛЕКЦИЯ')
    process_one(encoder)
    client.post('/admin/collections/2/access',data={'csrf':token,'access_level':'registered'})
    token = login(client, 'reader')
    assert client.get(f'/documents/{did}').status_code == 200
    with db.begin() as c:
        c.execute(text("UPDATE docsearch.collections SET access_level='private' WHERE collection_id=2"))
    assert client.get(f'/documents/{did}').status_code == 404
    assert client.get(f'/documents/{did}/download').status_code == 404
    for mode in ['vector','lexical','hybrid']:
        assert 'ОТКРЫВАЕМАЯ КОЛЛЕКЦИЯ' not in client.get('/',params={'q':'резервная копия','mode':mode}).text
    with db.begin() as c:
        c.execute(text('INSERT INTO docsearch.collection_access VALUES (3,2)'))
    assert client.get(f'/documents/{did}').status_code == 200
    token = login(client)
    for level in ['registered','private']:
        assert client.post('/admin/collections/2/access',data={'csrf':token,'access_level':level}).status_code == 200
    login(client, 'reader')
    assert client.get(f'/documents/{did}/download').status_code == 200


def test_access_settings_require_admin_and_csrf(client):
    token = login(client, 'reader')
    assert client.post('/admin/collections/1/access',data={'csrf':token,'access_level':'registered'}).status_code == 403
    token = login(client)
    assert client.post('/admin/collections/1/access',data={'csrf':'wrong','access_level':'registered'}).status_code == 403
    assert client.post('/admin/collections/1/access',data={'csrf':token,'access_level':'public'}).status_code == 400
    assert client.post('/admin/collections/999/access',data={'csrf':token,'access_level':'private'}).status_code == 404
    assert client.post('/admin/collections',data={'csrf':token,'name':'Общая','access_level':'registered'}).status_code == 200
    assert 'Для всех зарегистрированных' in client.get('/admin').text


def test_shared_collection_still_requires_login(client, db):
    token = login(client)
    did = upload(client,token)
    client.post('/admin/collections/1/access',data={'csrf':token,'access_level':'registered'})
    client.post('/logout',data={'csrf':token})
    for url in ['/',f'/documents/{did}',f'/documents/{did}/download','/?q=резервная']:
        r = client.get(url,follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'] == '/login'


def test_upgrade_0001_preserves_documents_and_grants(client, db):
    token = login(client)
    did = upload(client,token)
    with db.begin() as c:
        c.execute(text('ALTER TABLE docsearch.collections DROP COLUMN access_level'))
        c.execute(text("UPDATE public.alembic_version SET version_num='0001'"))
    command.upgrade(Config('alembic.ini'),'head')
    command.upgrade(Config('alembic.ini'),'head')
    with db.connect() as c:
        assert c.execute(text('SELECT access_level FROM docsearch.collections WHERE collection_id=1')).scalar_one() == 'private'
        assert c.execute(text('SELECT count(*) FROM docsearch.collection_access')).scalar_one() == 2
        assert c.execute(text('SELECT document_id FROM docsearch.documents')).scalar_one() == did
        assert c.execute(text('SELECT version_num FROM public.alembic_version')).scalar_one() == '0002'
