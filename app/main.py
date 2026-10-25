import logging
import re
import secrets
from pathlib import Path
from urllib.parse import urlencode
from fastapi import FastAPI, Request, HTTPException, Form, File, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.middleware.sessions import SessionMiddleware
from .config import settings, ROOT
from .db import engine, rows, row
from .security import (csrf_token, check_csrf, require_user, current_user, require_admin,
                       require_editor, check_collection, verify_password, password_hash, login_limit)
from .documents import stored_file
from .search import search
from .middleware import RequestGuards

log = logging.getLogger(__name__)
app = FastAPI(title='Поиск по документам', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings().session_secret, session_cookie='docsearch',
                   same_site='lax', https_only=settings().cookie_secure, max_age=8*3600)
app.add_middleware(RequestGuards, max_bytes=(settings().max_upload_mb + 2)*1024**2)
app.mount('/static', StaticFiles(directory=ROOT / 'app' / 'static'), name='static')
templates = Jinja2Templates(directory=ROOT / 'app' / 'templates')
STATUS = {'pending': 'В очереди', 'processing': 'Обрабатывается', 'ready': 'Готов', 'error': 'Ошибка'}
ROLE = {'reader': 'Читатель', 'editor': 'Редактор', 'admin': 'Администратор'}

def render(request, template, user=None, status=200, **context):
    return templates.TemplateResponse(request=request, name=template,
        context={'user': user, 'csrf': csrf_token(request), 'statuses': STATUS, 'roles': ROLE,
                 'message': request.query_params.get('message', '')[:300], **context}, status_code=status)

def redirect(path, message=''):
    if message:
        path += ('&' if '?' in path else '?') + urlencode({'message': message})
    return RedirectResponse(path, status_code=303)

def collections_for(user):
    return rows('''SELECT c.* FROM docsearch.collections c WHERE :admin OR c.access_level='registered' OR EXISTS
      (SELECT 1 FROM docsearch.collection_access ac WHERE ac.collection_id=c.collection_id AND ac.user_id=:uid)
      ORDER BY c.name''', {'admin': user['role']=='admin', 'uid': user['user_id']})

def categories_for(user):
    if user['role'] in ('admin','editor'):
        return rows('SELECT * FROM docsearch.categories ORDER BY name')
    return rows('''SELECT DISTINCT cat.* FROM docsearch.categories cat
      JOIN docsearch.document_categories dc USING(category_id)
      JOIN docsearch.documents d USING(document_id)
      JOIN docsearch.collections c ON c.collection_id=d.collection_id
      WHERE c.access_level='registered' OR EXISTS (
        SELECT 1 FROM docsearch.collection_access ac
        WHERE ac.collection_id=d.collection_id AND ac.user_id=:uid)
      ORDER BY cat.name''', {'uid': user['user_id']})

def get_document(user, did):
    doc = row('SELECT * FROM docsearch.documents WHERE document_id=:id', {'id': did})
    if not doc:
        raise HTTPException(404, 'Документ не найден.')
    check_collection(user, doc['collection_id'])
    return doc

@app.exception_handler(HTTPException)
async def http_error(request, exc):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers['Location'], status_code=303)
    return render(request, 'error.html', user=current_user(request), status=exc.status_code,
                  error=str(exc.detail))

@app.exception_handler(RequestValidationError)
async def invalid_form(request, exc):
    return render(request, 'error.html', user=current_user(request), status=422,
                  error='Проверьте поля формы: отсутствует значение или неверный формат.')

@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    return render(request, 'error.html', user=current_user(request), status=409,
                  error='Такая запись уже существует или изменение нарушает связи данных.')

@app.exception_handler(OperationalError)
async def unavailable_db(request, exc):
    log.error('Database connection unavailable')
    return render(request, 'error.html', status=503,
                  error='База данных недоступна. Проверьте запуск PostgreSQL и настройки подключения.')

@app.get('/health', response_class=PlainTextResponse)
def health():
    with engine().connect() as c:
        c.execute(text('SELECT 1 FROM docsearch.roles LIMIT 1'))
    return 'ok'

@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return redirect('/')
    return render(request, 'login.html')

@app.get('/register', response_class=HTMLResponse)
def register_page(request: Request):
    if current_user(request):
        return redirect('/')
    return render(request, 'register.html')

@app.post('/register')
def register(request: Request, login: str = Form(..., max_length=100),
             display_name: str = Form(..., max_length=200),
             password: str = Form(..., max_length=256),
             password_confirm: str = Form(..., max_length=256), csrf: str = Form(...)):
    check_csrf(request, csrf)
    if current_user(request):
        return redirect('/')
    login_limit(request, 'register')
    login = login.strip().lower()
    display_name = display_name.strip()
    def invalid(message, status=400):
        return render(request, 'register.html', status=status, error=message,
                      login_value=login, name_value=display_name)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,99}', login):
        return invalid('Логин: от 3 до 100 символов. Латинские буквы, цифры, точка, дефис и подчёркивание; первый символ — буква или цифра.')
    if not display_name:
        return invalid('Введите отображаемое имя.')
    if password != password_confirm:
        return invalid('Пароли не совпадают.')
    try:
        encoded = password_hash(password)
    except ValueError as exc:
        return invalid(str(exc))
    with engine().begin() as c:
        # Роль задаётся сервером: поля формы не могут повысить привилегии.
        # ON CONFLICT также обрабатывает одновременную регистрацию одного логина.
        uid = c.execute(text('''INSERT INTO docsearch.users
            (login,password_hash,display_name,role_id)
            SELECT :login,:hash,:name,role_id FROM docsearch.roles WHERE code='reader'
            ON CONFLICT (login) DO NOTHING
            RETURNING user_id'''), {'login':login,'hash':encoded,'name':display_name}).scalar_one_or_none()
    if uid is None:
        return invalid('Этот логин уже занят. Выберите другой.', 409)
    request.session.clear()
    request.session['user_id'] = uid
    csrf_token(request)
    return redirect('/', 'Регистрация завершена. Вам доступны общие коллекции; доступ к закрытым выдаёт администратор.')

@app.post('/login')
def login(request: Request, login: str = Form(..., max_length=100),
          password: str = Form(..., max_length=256), csrf: str = Form(...)):
    check_csrf(request, csrf); login_limit(request)
    account = row('SELECT * FROM docsearch.users WHERE login=:login', {'login': login.strip().lower()})
    valid = verify_password(account['password_hash'] if account else None, password)
    if not account or not valid or not account['is_active']:
        return render(request, 'login.html', status=401, error='Неверный логин или пароль.')
    request.session.clear()
    request.session['user_id'] = account['user_id']
    csrf_token(request)
    return redirect('/')

@app.post('/logout')
def logout(request: Request, csrf: str = Form(...)):
    check_csrf(request, csrf); request.session.clear()
    return redirect('/login')

@app.get('/', response_class=HTMLResponse)
def home(request: Request, q: str = '', mode: str = 'vector', k: int = 10,
         collection: int = 0, category: int = 0, page: int = 1):
    user = require_user(request)
    if page < 1 or collection < 0 or category < 0:
        raise HTTPException(400, 'Неверные параметры страницы.')
    if collection:
        check_collection(user, collection)
    params = {'admin': user['role']=='admin', 'uid': user['user_id'], 'offset': (page-1)*20,
              'collection': collection, 'category': category}
    docs = rows('''SELECT d.document_id,d.title,d.file_format,d.processing_status,d.created_at,c.name AS collection_name
        FROM docsearch.documents d JOIN docsearch.collections c USING(collection_id)
        WHERE (:admin OR c.access_level='registered' OR EXISTS(SELECT 1 FROM docsearch.collection_access a
             WHERE a.user_id=:uid AND a.collection_id=d.collection_id))
        AND (:collection=0 OR d.collection_id=:collection)
        AND (:category=0 OR EXISTS (SELECT 1 FROM docsearch.document_categories dc
             WHERE dc.document_id=d.document_id AND dc.category_id=:category))
        ORDER BY d.document_id DESC LIMIT 21 OFFSET :offset''', params)
    results, error = None, None
    if q:
        try:
            results = search(user, q, mode, k, collection, category)
        except ValueError as exc:
            error = str(exc)
        except Exception:
            log.exception('Search failed')
            error = 'Не удалось выполнить поиск. Проверьте загрузку модели и журнал приложения. Можно выбрать поиск по словам.'
    return render(request, 'home.html', user=user, collections=collections_for(user),
        categories=categories_for(user), documents=docs[:20], has_next=len(docs)>20,
        q=q, mode=mode, k=k, collection=collection, category=category, page=page,
        next_query=urlencode({'page':page+1,'collection':collection,'category':category}),
        prev_query=urlencode({'page':max(1,page-1),'collection':collection,'category':category}),
        results=results, error=error)

@app.get('/upload', response_class=HTMLResponse)
def upload_page(request: Request):
    user = require_user(request); require_editor(user)
    return render(request, 'upload.html', user=user, collections=collections_for(user),
                  categories=categories_for(user), max_mb=settings().max_upload_mb)

@app.post('/upload')
def upload(request: Request, collection_id: int = Form(...), title: str = Form('', max_length=500),
           category_ids: list[int] = Form([]), csrf: str = Form(...), file: UploadFile = File(...)):
    user = require_user(request); require_editor(user); check_csrf(request, csrf)
    check_collection(user, collection_id)
    filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
    extension = Path(filename).suffix.lower().lstrip('.')
    if extension not in ('txt','docx','pdf') or not filename or len(filename)>500:
        raise HTTPException(400, 'Выберите TXT, DOCX или PDF с текстовым слоем.')
    title = title.strip() or Path(filename).stem[:500]
    if not title:
        raise HTTPException(400, 'Введите название документа.')
    permitted = {c['category_id'] for c in categories_for(user)}
    if not set(category_ids) <= permitted:
        raise HTTPException(400, 'Выбрана недоступная категория.')
    key = secrets.token_hex(24) + '.' + extension
    path = stored_file(key); path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with path.open('xb') as out:
            while block := file.file.read(1024*256):
                size += len(block)
                if size > settings().max_upload_mb * 1024**2:
                    raise HTTPException(413, 'Файл превышает допустимый размер.')
                out.write(block)
        if not size:
            raise HTTPException(400, 'Выбран пустой файл.')
        with engine().begin() as c:
            did = c.execute(text('''INSERT INTO docsearch.documents
                (collection_id,uploaded_by,title,original_filename,storage_key,file_format)
                VALUES (:collection,:uid,:title,:filename,:key,:format) RETURNING document_id'''),
                {'collection':collection_id,'uid':user['user_id'],'title':title,'filename':filename,
                 'key':key,'format':extension}).scalar_one()
            for cid in set(category_ids):
                c.execute(text('INSERT INTO docsearch.document_categories VALUES (:did,:cid)'), {'did':did,'cid':cid})
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()
    return redirect('/documents/'+str(did), 'Документ добавлен в очередь обработки.')

@app.get('/documents/{did}', response_class=HTMLResponse)
def document_page(request: Request, did: int, page: int = 1, chunk: int = 0):
    user = require_user(request); doc = get_document(user, did)
    if page < 1:
        raise HTTPException(400, 'Неверный номер страницы.')
    if chunk:
        selected = row('SELECT chunk_number FROM docsearch.document_chunks WHERE chunk_id=:cid AND document_id=:did', {'cid':chunk,'did':did})
        if selected:
            page = (selected['chunk_number']-1)//10 + 1
    chunks = rows('''SELECT chunk_id,chunk_number,content,page_number FROM docsearch.document_chunks
        WHERE document_id=:id ORDER BY chunk_number LIMIT 11 OFFSET :offset''', {'id':did,'offset':(page-1)*10})
    categories = rows('''SELECT cat.* FROM docsearch.categories cat
        JOIN docsearch.document_categories dc USING(category_id) WHERE dc.document_id=:id ORDER BY cat.name''', {'id':did})
    return render(request, 'document.html', user=user, document=doc, chunks=chunks[:10],
                  has_next=len(chunks)>10, page=page, categories=categories, all_categories=categories_for(user))

@app.get('/documents/{did}/download')
def download(request: Request, did: int):
    user = require_user(request); doc = get_document(user, did)
    try:
        path = stored_file(doc['storage_key'])
    except ValueError:
        raise HTTPException(404, 'Исходный файл недоступен.')
    if not path.is_file():
        raise HTTPException(404, 'Исходный файл не найден в хранилище.')
    return FileResponse(path, filename=doc['original_filename'], media_type='application/octet-stream',
                        headers={'X-Content-Type-Options':'nosniff'})

@app.post('/documents/{did}/edit')
def edit_document(request: Request, did: int, title: str = Form(..., max_length=500),
                  category_ids: list[int] = Form([]), csrf: str = Form(...)):
    user = require_user(request); require_editor(user); check_csrf(request, csrf)
    get_document(user, did)
    if not title.strip():
        raise HTTPException(400, 'Название не может быть пустым.')
    permitted = {c['category_id'] for c in categories_for(user)}
    if not set(category_ids) <= permitted:
        raise HTTPException(400, 'Выбрана недоступная категория.')
    with engine().begin() as c:
        c.execute(text('UPDATE docsearch.documents SET title=:title WHERE document_id=:id'), {'title':title.strip(),'id':did})
        c.execute(text('DELETE FROM docsearch.document_categories WHERE document_id=:id'), {'id':did})
        for cid in set(category_ids):
            c.execute(text('INSERT INTO docsearch.document_categories VALUES (:did,:cid)'), {'did':did,'cid':cid})
    return redirect('/documents/'+str(did), 'Название и категории сохранены.')

@app.post('/documents/{did}/reindex')
def reindex(request: Request, did: int, csrf: str = Form(...)):
    user = require_user(request); require_editor(user); check_csrf(request, csrf)
    get_document(user, did)
    with engine().begin() as c:
        c.execute(text("UPDATE docsearch.documents SET processing_status='pending',error_message=NULL WHERE document_id=:id AND processing_status IN ('ready','error')"), {'id':did})
    return redirect('/documents/'+str(did), 'Если обработка ещё не шла, документ добавлен в очередь.')

@app.post('/documents/{did}/delete')
def delete_document(request: Request, did: int, csrf: str = Form(...)):
    user = require_user(request); require_editor(user); check_csrf(request, csrf)
    doc = get_document(user, did)
    with engine().begin() as c:
        c.execute(text('DELETE FROM docsearch.documents WHERE document_id=:id'), {'id':did})
    try:
        stored_file(doc['storage_key']).unlink(missing_ok=True)
    except (ValueError, OSError):
        log.warning('Could not remove source file for document %s', did)
    return redirect('/', 'Документ удалён.')

@app.get('/admin', response_class=HTMLResponse)
def admin_page(request: Request):
    user = require_user(request); require_admin(user)
    return render(request, 'admin.html', user=user,
        users=rows('SELECT u.user_id,u.login,u.display_name,u.is_active,r.code AS role FROM docsearch.users u JOIN docsearch.roles r USING(role_id) ORDER BY u.user_id'),
        collections=collections_for(user), categories=categories_for(user),
        grants=rows('''SELECT ac.user_id,ac.collection_id,u.login,c.name FROM docsearch.collection_access ac
            JOIN docsearch.users u USING(user_id) JOIN docsearch.collections c USING(collection_id)
            ORDER BY u.login,c.name'''))

@app.post('/admin/users')
def add_user(request: Request, login: str = Form(..., max_length=100), display_name: str = Form(..., max_length=200),
             password: str = Form(..., max_length=256), role: str = Form(...), csrf: str = Form(...)):
    user = require_user(request); require_admin(user); check_csrf(request, csrf)
    if role not in ROLE or not login.strip() or not display_name.strip():
        raise HTTPException(400, 'Проверьте логин, имя и роль.')
    try:
        encoded = password_hash(password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with engine().begin() as c:
        c.execute(text('''INSERT INTO docsearch.users(login,password_hash,display_name,role_id)
            SELECT :login,:password,:name,role_id FROM docsearch.roles WHERE code=:role'''),
            {'login':login.strip().lower(),'password':encoded,'name':display_name.strip(),'role':role})
    return redirect('/admin', 'Пользователь создан. Назначьте ему доступ к коллекциям.')

@app.post('/admin/users/{uid}/role')
def change_user_role(request: Request, uid: int, role: str = Form(...), csrf: str = Form(...)):
    user = require_user(request); require_admin(user); check_csrf(request, csrf)
    if role not in ROLE:
        raise HTTPException(400, 'Неизвестная роль.')
    if uid == user['user_id']:
        raise HTTPException(400, 'Свою роль изменить нельзя. Обратитесь к другому администратору.')
    with engine().begin() as c:
        changed = c.execute(text('''UPDATE docsearch.users u SET role_id=r.role_id
            FROM docsearch.roles r WHERE u.user_id=:uid AND r.code=:role
            RETURNING u.user_id'''), {'uid':uid,'role':role}).scalar_one_or_none()
    if changed is None:
        raise HTTPException(404, 'Пользователь не найден.')
    return redirect('/admin', 'Роль пользователя изменена. Новые права действуют со следующего запроса.')

@app.post('/admin/users/{uid}/toggle')
def toggle_user(request: Request, uid: int, csrf: str = Form(...)):
    user = require_user(request); require_admin(user); check_csrf(request, csrf)
    if user['user_id'] == uid:
        raise HTTPException(400, 'Нельзя отключить собственную учётную запись.')
    with engine().begin() as c:
        c.execute(text('UPDATE docsearch.users SET is_active=NOT is_active WHERE user_id=:id'), {'id':uid})
    return redirect('/admin', 'Состояние пользователя изменено.')

@app.post('/admin/collections')
def add_collection(request: Request, name: str = Form(..., max_length=200), description: str = Form('', max_length=3000),
                   access_level: str = Form('private'), csrf: str = Form(...)):
    user = require_user(request); require_admin(user); check_csrf(request, csrf)
    if not name.strip():
        raise HTTPException(400, 'Введите название коллекции.')
    if access_level not in ('private', 'registered'):
        raise HTTPException(400, 'Неизвестный режим доступа.')
    with engine().begin() as c:
        c.execute(text('INSERT INTO docsearch.collections(name,description,access_level) VALUES (:name,:description,:access)'),
                  {'name':name.strip(),'description':description.strip(),'access':access_level})
    return redirect('/admin', 'Коллекция создана.')

@app.post('/admin/collections/{cid}/access')
def change_collection_access(request: Request, cid: int, access_level: str = Form(...), csrf: str = Form(...)):
    user = require_user(request); require_admin(user); check_csrf(request, csrf)
    if access_level not in ('private', 'registered'):
        raise HTTPException(400, 'Неизвестный режим доступа.')
    with engine().begin() as c:
        changed = c.execute(text('''UPDATE docsearch.collections SET access_level=:level
            WHERE collection_id=:id RETURNING collection_id'''), {'level':access_level,'id':cid}).scalar_one_or_none()
    if changed is None:
        raise HTTPException(404, 'Коллекция не найдена.')
    return redirect('/admin', 'Режим доступа сохранён. Индивидуальные разрешения не изменены.')

@app.post('/admin/categories')
def add_category(request: Request, name: str = Form(..., max_length=150), csrf: str = Form(...)):
    user = require_user(request); require_admin(user); check_csrf(request, csrf)
    if not name.strip():
        raise HTTPException(400, 'Введите название категории.')
    with engine().begin() as c:
        c.execute(text('INSERT INTO docsearch.categories(name) VALUES (:name)'), {'name':name.strip()})
    return redirect('/admin', 'Категория создана.')

@app.post('/admin/access')
def grant_access(request: Request, user_id: int = Form(...), collection_id: int = Form(...),
                 action: str = Form('grant'), csrf: str = Form(...)):
    user = require_user(request); require_admin(user); check_csrf(request, csrf)
    if action not in ('grant','revoke'):
        raise HTTPException(400, 'Неизвестное действие.')
    with engine().begin() as c:
        if action == 'grant':
            c.execute(text('INSERT INTO docsearch.collection_access VALUES (:uid,:cid) ON CONFLICT DO NOTHING'), {'uid':user_id,'cid':collection_id})
        else:
            c.execute(text('DELETE FROM docsearch.collection_access WHERE user_id=:uid AND collection_id=:cid'), {'uid':user_id,'cid':collection_id})
    return redirect('/admin', 'Права доступа обновлены.')
