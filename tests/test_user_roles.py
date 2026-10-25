import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient
from app.main import app
from test_application import csrf, login


@pytest.mark.parametrize('role', ['editor','admin','reader'])
def test_admin_changes_role_without_changing_account(client, db, role):
    token = login(client)
    with db.connect() as c:
        before = dict(c.execute(text('SELECT * FROM docsearch.users WHERE user_id=3')).mappings().one())
    r = client.post('/admin/users/3/role',data={'csrf':token,'role':role},follow_redirects=False)
    assert r.status_code == 303
    with db.connect() as c:
        after = dict(c.execute(text('SELECT * FROM docsearch.users WHERE user_id=3')).mappings().one())
        actual = c.execute(text('SELECT r.code FROM docsearch.users u JOIN docsearch.roles r USING(role_id) WHERE u.user_id=3')).scalar_one()
        assert actual == role
        assert {k:v for k,v in before.items() if k!='role_id'} == {k:v for k,v in after.items() if k!='role_id'}
        assert c.execute(text('SELECT count(*) FROM docsearch.collection_access WHERE user_id=3')).scalar_one()==1


def test_existing_session_gains_and_loses_role_immediately(client, db):
    token = login(client)
    with TestClient(app) as other:
        login(other,'reader')
        assert other.get('/upload').status_code==403
        client.post('/admin/users/3/role',data={'csrf':token,'role':'editor'})
        assert other.get('/upload').status_code==200
        assert other.get('/admin').status_code==403
        client.post('/admin/users/3/role',data={'csrf':token,'role':'admin'})
        assert other.get('/admin').status_code==200
        client.post('/admin/users/3/role',data={'csrf':token,'role':'reader'})
        assert other.get('/admin').status_code==403
        assert other.get('/upload').status_code==403


def test_role_changes_require_admin_and_csrf(client):
    for name in ['reader','editor']:
        token=login(client,name)
        assert client.post('/admin/users/3/role',data={'csrf':token,'role':'admin'}).status_code==403
    token=login(client)
    assert client.post('/admin/users/3/role',data={'csrf':'wrong','role':'admin'}).status_code==403
    assert client.post('/admin/users/3/role',data={'csrf':token,'role':'superadmin'}).status_code==400
    assert client.post('/admin/users/999/role',data={'csrf':token,'role':'reader'}).status_code==404
    assert client.post('/admin/users/1/role',data={'csrf':token,'role':'reader'}).status_code==400
    page=client.get('/admin')
    assert 'action="/admin/users/3/role"' in page.text
    assert 'action="/admin/users/1/role"' not in page.text
    client.post('/logout',data={'csrf':csrf(page)})
    assert client.post('/admin/users/3/role',data={'csrf':token,'role':'admin'},follow_redirects=False).status_code==303
