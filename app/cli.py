import argparse
import getpass
from sqlalchemy import text
from .db import engine
from .security import password_hash

def main():
    parser = argparse.ArgumentParser(description='Управление поисковой системой')
    parser.add_argument('command', choices=['create-admin', 'reset-password', 'download-model'])
    args = parser.parse_args()
    if args.command == 'download-model':
        from .embeddings import get_encoder
        model = get_encoder()
        print('Модель готова:', model.model_name, model.revision, model.dimension)
        return
    login = input('Логин: ').strip().lower()
    if not login or len(login)>100:
        raise SystemExit('Логин должен содержать от 1 до 100 символов.')
    name = input('Отображаемое имя: ').strip() if args.command == 'create-admin' else ''
    password = getpass.getpass('Пароль (от 10 символов): ')
    if password != getpass.getpass('Повторите пароль: '):
        raise SystemExit('Пароли не совпадают.')
    encoded = password_hash(password)
    with engine().begin() as c:
        exists = c.execute(text('SELECT user_id FROM docsearch.users WHERE login=:login'), {'login':login}).scalar_one_or_none()
        if args.command == 'create-admin':
            if exists is not None:
                raise SystemExit('Этот логин уже занят. Для смены пароля есть reset-password.')
            if not name or len(name)>200:
                raise SystemExit('Имя должно содержать от 1 до 200 символов.')
            c.execute(text('''INSERT INTO docsearch.users(login,password_hash,display_name,role_id)
              SELECT :login,:password,:name,role_id FROM docsearch.roles WHERE code='admin' '''),
              {'login':login,'password':encoded,'name':name})
            print('Администратор создан.')
        else:
            if exists is None:
                raise SystemExit('Пользователь не найден.')
            c.execute(text('UPDATE docsearch.users SET password_hash=:p WHERE user_id=:id'), {'p':encoded,'id':exists})
            print('Пароль изменён. Для прекращения уже выданных сессий смените SESSION_SECRET и перезапустите web.')

if __name__ == '__main__':
    main()
