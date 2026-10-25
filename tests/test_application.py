import os
import io
import importlib.util
from bs4 import BeautifulSoup
from sqlalchemy import text
from alembic.config import Config
from alembic import command
import pytest
from app.documents import stored_file

def csrf(response):
    node=BeautifulSoup(response.text,'html.parser').select_one('input[name=csrf]')
    assert node is not None,response.text[:500]
    return node['value']

def login(client,name='admin'):
    token=csrf(client.get('/login'))
    result=client.post('/login',data={'login':name,'password':'Testing-password-123','csrf':token},follow_redirects=False)
    assert result.status_code==303
    return csrf(client.get('/'))

def upload(client,token,collection=1,title='Резервное копирование',content='Резервная копия PostgreSQL позволяет восстановить базу данных.'):
    r=client.post('/upload',data={'csrf':token,'collection_id':collection,'title':title},
        files={'file':('guide.txt',content.encode(),'text/plain')},follow_redirects=False)
    assert r.status_code==303,r.text
    return int(r.headers['location'].split('?')[0].rsplit('/',1)[-1])

def test_authentication_and_no_api(client):
    assert client.get('/',follow_redirects=False).status_code==303
    assert client.get('/docs').status_code==404
    assert client.get('/openapi.json').status_code==404
    token=csrf(client.get('/login'))
    assert client.post('/login',data={'login':'admin','password':'wrong','csrf':token}).status_code==401
    token=login(client)
    assert 'text/html' in client.get('/').headers['content-type']
    assert client.post('/logout',data={'csrf':token},follow_redirects=False).status_code==303
    assert client.get('/',follow_redirects=False).status_code==303

def test_csrf(client):
    login(client)
    r=client.post('/admin/collections',data={'csrf':'wrong','name':'Bad'})
    assert r.status_code==403

def test_reader_cannot_write(client):
    token=login(client,'reader')
    assert client.get('/admin').status_code==403
    assert client.get('/upload').status_code==403
    assert client.post('/upload',data={'csrf':token,'collection_id':1},files={'file':('a.txt',b'a')}).status_code==403

def test_upload_index_search_download_delete(client,db,encoder,monkeypatch):
    from app.worker import process_one
    from app import search as search_module
    monkeypatch.setattr(search_module,'get_encoder',lambda:encoder)
    token=login(client,'editor');did=upload(client,token)
    assert process_one(encoder)
    page=client.get(f'/documents/{did}')
    assert page.status_code==200 and 'Резервная копия' in page.text and 'Готов' in page.text
    for mode in ['lexical','vector','hybrid']:
        r=client.get('/',params={'q':'резервная копия','mode':mode})
        assert r.status_code==200 and 'class="panel result"' in r.text,r.text
    data=client.get(f'/documents/{did}/download')
    assert data.status_code==200 and data.content.decode().startswith('Резервная копия')
    assert client.post(f'/documents/{did}/delete',data={'csrf':token},follow_redirects=False).status_code==303
    with db.connect() as c:
        assert c.execute(text('SELECT count(*) FROM docsearch.chunk_embeddings')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM docsearch.document_chunks')).scalar_one()==0

def test_collection_isolation_and_revocation(client,db,encoder,monkeypatch):
    from app.worker import process_one
    from app import search as sm
    monkeypatch.setattr(sm,'get_encoder',lambda:encoder)
    token=login(client);did=upload(client,token,2,title='СЕКРЕТНОЕ НАЗВАНИЕ')
    assert process_one(encoder)
    login(client,'reader')
    assert 'СЕКРЕТНОЕ НАЗВАНИЕ' not in client.get('/').text
    for path in [f'/documents/{did}',f'/documents/{did}/download']:
        assert client.get(path).status_code==404
    for mode in ['lexical','vector','hybrid']:
        result=client.get('/',params={'q':'резервная копия','mode':mode})
        assert 'СЕКРЕТНОЕ НАЗВАНИЕ' not in result.text
    with db.begin() as c:c.execute(text('INSERT INTO docsearch.collection_access VALUES (3,2)'))
    assert client.get(f'/documents/{did}').status_code==200
    with db.begin() as c:c.execute(text('DELETE FROM docsearch.collection_access WHERE user_id=3 AND collection_id=2'))
    assert client.get(f'/documents/{did}/download').status_code==404

def test_xss_is_escaped(client):
    token=login(client)
    did=upload(client,token,title='<script>alert(1)</script>')
    page=client.get(f'/documents/{did}')
    assert '<script>alert(1)</script>' not in page.text
    assert '&lt;script&gt;' in page.text
    assert "script-src 'none'" in page.headers['content-security-policy']

def test_document_metadata_does_not_recompute(client,db,encoder):
    from app.worker import process_one
    token=login(client);did=upload(client,token)
    process_one(encoder)
    with db.connect() as c:before=c.execute(text('SELECT count(*) FROM docsearch.chunk_embeddings')).scalar_one()
    assert client.post(f'/documents/{did}/edit',data={'csrf':token,'title':'Новое название','category_ids':'1'},follow_redirects=False).status_code==303
    with db.connect() as c:
        assert c.execute(text('SELECT count(*) FROM docsearch.chunk_embeddings')).scalar_one()==before
        assert c.execute(text('SELECT processing_status FROM docsearch.documents')).scalar_one()=='ready'

def test_worker_failure_and_retry(client,db,encoder):
    from app.worker import process_one
    token=login(client);did=upload(client,token,content='   ')
    process_one(encoder)
    with db.connect() as c:assert c.execute(text('SELECT processing_status FROM docsearch.documents')).scalar_one()=='error'
    assert client.post(f'/documents/{did}/reindex',data={'csrf':token},follow_redirects=False).status_code==303
    with db.connect() as c:assert c.execute(text('SELECT processing_status FROM docsearch.documents')).scalar_one()=='pending'

def test_deactivated_session(client,db):
    login(client,'reader')
    with db.begin() as c:c.execute(text("UPDATE docsearch.users SET is_active=FALSE WHERE login='reader'"))
    assert client.get('/',follow_redirects=False).status_code==303

def test_admin_forms_and_new_editor_categories(client,db):
    token=login(client)
    for url,data in [('/admin/collections',{'name':'Новая коллекция'}),('/admin/categories',{'name':'Новая категория'}),
        ('/admin/users',{'login':'newreader','display_name':'Новый читатель','password':'safe-new-password','role':'reader'})]:
        assert client.post(url,data={'csrf':token,**data},follow_redirects=False).status_code==303
    assert client.post('/admin/access',data={'csrf':token,'user_id':4,'collection_id':3,'action':'grant'},follow_redirects=False).status_code==303
    login(client,'editor')
    assert 'Новая категория' in client.get('/upload').text

def test_partial_model_not_searchable(client,db,encoder):
    from app.worker import process_one
    from app.search import search
    token=login(client);did=upload(client,token,content=('Резервная копия базы. '*200))
    process_one(encoder)
    with db.begin() as c:
        c.execute(text('DELETE FROM docsearch.chunk_embeddings WHERE chunk_id=(SELECT max(chunk_id) FROM docsearch.document_chunks)'))
    assert search({'role':'admin','user_id':1},'резервная копия','vector',encoder=encoder)==[]

def test_bad_upload_and_body_limit(client):
    token=login(client)
    assert client.post('/upload',data={'csrf':token,'collection_id':1},files={'file':('bad.exe',b'bad')}).status_code==400
    assert client.post('/upload',data={'csrf':token,'collection_id':1},files={'file':('empty.txt',b'')}).status_code==400
    assert client.post('/upload',content=b'a'*(13*1024**2)).status_code==413

def test_migrations_adopt_existing_and_preserve_data(db,accounts):
    with db.begin() as c:
        c.execute(text('ALTER TABLE docsearch.collections DROP COLUMN access_level'))
        c.execute(text('DELETE FROM public.alembic_version'))
    command.upgrade(Config('alembic.ini'),'head')
    command.upgrade(Config('alembic.ini'),'head')
    with db.connect() as c:
        assert c.execute(text('SELECT count(*) FROM docsearch.users')).scalar_one()==3
        assert c.execute(text('SELECT version_num FROM public.alembic_version')).scalar_one()=='0002'

def test_migration_rejects_schema_drift(db):
    with db.begin() as c:
        c.execute(text('ALTER TABLE docsearch.collections DROP COLUMN access_level'))
        c.execute(text('DELETE FROM public.alembic_version'))
        c.execute(text('ALTER TABLE docsearch.users ADD COLUMN unexpected TEXT'))
    try:
        with pytest.raises(RuntimeError,match='отличается'):
            command.upgrade(Config('alembic.ini'),'head')
    finally:
        with db.begin() as c:c.execute(text('ALTER TABLE docsearch.users DROP COLUMN unexpected'))
        command.upgrade(Config('alembic.ini'),'head')

@pytest.mark.real_model
def test_real_model_processing_and_search(client,db):
    if os.environ.get('RUN_REAL_MODEL')!='1':pytest.skip('RUN_REAL_MODEL=1 для проверки настоящей модели')
    from app.worker import process_one
    from app.search import search
    token=login(client)
    did=upload(client,token,content='Резервное копирование PostgreSQL. Чтобы восстановить базу данных после сбоя, используйте ранее созданную резервную копию и программу pg_restore.')
    process_one()
    result=search({'role':'admin','user_id':1},'Как вернуть базу данных после аварии?','vector')
    assert result and result[0]['document_id']==did
    with db.connect() as c:
        assert c.execute(text('SELECT public.vector_dims(embedding) FROM docsearch.chunk_embeddings LIMIT 1')).scalar_one()==384

def test_login_throttle(client):
    token=csrf(client.get('/login'))
    for _ in range(15):
        assert client.post('/login',data={'csrf':token,'login':'missing','password':'wrong'}).status_code==401
    assert client.post('/login',data={'csrf':token,'login':'missing','password':'wrong'}).status_code==429
