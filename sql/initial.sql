DO $check_extension$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        RAISE EXCEPTION 'Расширение pgvector не установлено на сервере PostgreSQL.'
            USING HINT = 'Установите pgvector для вашей версии сервера, затем повторите запуск. Одного pgAdmin недостаточно.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'vector' AND n.nspname <> 'public'
    ) THEN
        RAISE EXCEPTION 'pgvector уже подключён в другой схеме.'
            USING HINT = 'Этот файл рассчитан на pgvector в public. Используйте отдельную пустую БД либо адаптируйте квалифицированные имена.';
    END IF;
END;
$check_extension$;

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE SCHEMA IF NOT EXISTS docsearch;

-- 2. Справочник ролей.
CREATE TABLE IF NOT EXISTS docsearch.roles (
    role_id SMALLINT GENERATED ALWAYS AS IDENTITY,
    code VARCHAR(30) NOT NULL,
    name VARCHAR(100) NOT NULL,
    CONSTRAINT pk_roles PRIMARY KEY (role_id),
    CONSTRAINT uq_roles_code UNIQUE (code),
    CONSTRAINT ck_roles_code CHECK (code ~ '^[a-z][a-z0-9_]*$'),
    CONSTRAINT ck_roles_name CHECK (btrim(name) <> '')
);

INSERT INTO docsearch.roles (code, name)
SELECT v.code, v.name
FROM (VALUES
    ('reader', 'Читатель'),
    ('editor', 'Редактор'),
    ('admin',  'Администратор')
) AS v(code, name)
WHERE NOT EXISTS (SELECT 1 FROM docsearch.roles r WHERE r.code = v.code)
ON CONFLICT (code) DO NOTHING;

-- 3. Пользователи приложения: у каждого ровно одна роль.
CREATE TABLE IF NOT EXISTS docsearch.users (
    user_id BIGINT GENERATED ALWAYS AS IDENTITY,
    login VARCHAR(100) NOT NULL,
    password_hash TEXT NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    role_id SMALLINT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT pk_users PRIMARY KEY (user_id),
    CONSTRAINT uq_users_login UNIQUE (login),
    CONSTRAINT fk_users_role FOREIGN KEY (role_id)
        REFERENCES docsearch.roles(role_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_users_login CHECK (btrim(login) <> '' AND login = lower(btrim(login))),
    CONSTRAINT ck_users_password_hash CHECK (btrim(password_hash) <> ''),
    CONSTRAINT ck_users_display_name CHECK (btrim(display_name) <> '')
);
-- Логины хранятся в нижнем регистре. Приложение нормализует ввод при входе.
-- CHECK проверяет наличие значения хеша, а не качество алгоритма хеширования.

-- 4. Коллекции документов.
CREATE TABLE IF NOT EXISTS docsearch.collections (
    collection_id BIGINT GENERATED ALWAYS AS IDENTITY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_collections PRIMARY KEY (collection_id),
    CONSTRAINT uq_collections_name UNIQUE (name),
    CONSTRAINT ck_collections_name CHECK (btrim(name) <> '')
);

-- 5. Доступ пользователя к коллекции (связь M:N).
CREATE TABLE IF NOT EXISTS docsearch.collection_access (
    user_id BIGINT NOT NULL,
    collection_id BIGINT NOT NULL,
    CONSTRAINT pk_collection_access PRIMARY KEY (user_id, collection_id),
    CONSTRAINT fk_collection_access_user FOREIGN KEY (user_id)
        REFERENCES docsearch.users(user_id) ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT fk_collection_access_collection FOREIGN KEY (collection_id)
        REFERENCES docsearch.collections(collection_id) ON DELETE CASCADE ON UPDATE RESTRICT
);
-- Строка означает доступ. Допустимые действия определяет роль пользователя.
-- Для администратора приложение предусматривает доступ ко всем коллекциям.

-- 6. Документы: каждый документ входит в одну коллекцию.
CREATE TABLE IF NOT EXISTS docsearch.documents (
    document_id BIGINT GENERATED ALWAYS AS IDENTITY,
    collection_id BIGINT NOT NULL,
    uploaded_by BIGINT NOT NULL,
    title VARCHAR(500) NOT NULL,
    original_filename VARCHAR(500) NOT NULL,
    storage_key TEXT NOT NULL,
    file_format VARCHAR(10) NOT NULL,
    extracted_text TEXT,
    processing_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_documents PRIMARY KEY (document_id),
    CONSTRAINT uq_documents_storage_key UNIQUE (storage_key),
    CONSTRAINT fk_documents_collection FOREIGN KEY (collection_id)
        REFERENCES docsearch.collections(collection_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_documents_uploaded_by FOREIGN KEY (uploaded_by)
        REFERENCES docsearch.users(user_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_documents_title CHECK (btrim(title) <> ''),
    CONSTRAINT ck_documents_filename CHECK (btrim(original_filename) <> ''),
    CONSTRAINT ck_documents_storage_key CHECK (btrim(storage_key) <> ''),
    CONSTRAINT ck_documents_format CHECK (file_format IN ('txt', 'docx', 'pdf')),
    CONSTRAINT ck_documents_status CHECK (processing_status IN ('pending', 'processing', 'ready', 'error')),
    CONSTRAINT ck_documents_error CHECK (
        (processing_status = 'error' AND error_message IS NOT NULL AND btrim(error_message) <> '')
        OR (processing_status <> 'error' AND error_message IS NULL)
    ),
    CONSTRAINT ck_documents_ready_text CHECK (
        processing_status <> 'ready' OR (extracted_text IS NOT NULL AND btrim(extracted_text) <> '')
    )
);
-- Название и исходное имя НЕ уникальны. Уникален адрес хранения файла.
-- Пользователя с загруженными документами не удаляем: можно установить is_active = FALSE.

-- 7. Категории и назначения категорий (M:N).
CREATE TABLE IF NOT EXISTS docsearch.categories (
    category_id BIGINT GENERATED ALWAYS AS IDENTITY,
    name VARCHAR(150) NOT NULL,
    description TEXT,
    CONSTRAINT pk_categories PRIMARY KEY (category_id),
    CONSTRAINT uq_categories_name UNIQUE (name),
    CONSTRAINT ck_categories_name CHECK (btrim(name) <> '')
);

CREATE TABLE IF NOT EXISTS docsearch.document_categories (
    document_id BIGINT NOT NULL,
    category_id BIGINT NOT NULL,
    CONSTRAINT pk_document_categories PRIMARY KEY (document_id, category_id),
    CONSTRAINT fk_document_categories_document FOREIGN KEY (document_id)
        REFERENCES docsearch.documents(document_id) ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT fk_document_categories_category FOREIGN KEY (category_id)
        REFERENCES docsearch.categories(category_id) ON DELETE CASCADE ON UPDATE RESTRICT
);

-- 8. Фрагменты. Позиции в extracted_text: с нуля, end_offset не включается.
-- Смещения измеряются в символах PostgreSQL, а не в байтах или UTF-16-единицах.
CREATE TABLE IF NOT EXISTS docsearch.document_chunks (
    chunk_id BIGINT GENERATED ALWAYS AS IDENTITY,
    document_id BIGINT NOT NULL,
    chunk_number INTEGER NOT NULL,
    content TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    page_number INTEGER,
    CONSTRAINT pk_document_chunks PRIMARY KEY (chunk_id),
    CONSTRAINT uq_document_chunks_number UNIQUE (document_id, chunk_number),
    CONSTRAINT fk_document_chunks_document FOREIGN KEY (document_id)
        REFERENCES docsearch.documents(document_id) ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT ck_document_chunks_number CHECK (chunk_number > 0),
    CONSTRAINT ck_document_chunks_content CHECK (btrim(content) <> ''),
    CONSTRAINT ck_document_chunks_offsets CHECK (start_offset >= 0 AND end_offset > start_offset),
    CONSTRAINT ck_document_chunks_length CHECK (char_length(content) = end_offset - start_offset),
    CONSTRAINT ck_document_chunks_page CHECK (page_number IS NULL OR page_number > 0)
);

-- 9. Неизменные профили моделей эмбеддингов.
CREATE TABLE IF NOT EXISTS docsearch.embedding_models (
    model_id BIGINT GENERATED ALWAYS AS IDENTITY,
    profile_name VARCHAR(150) NOT NULL,
    model_name TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    encoding_config JSONB NOT NULL DEFAULT '{}'::JSONB,
    CONSTRAINT pk_embedding_models PRIMARY KEY (model_id),
    CONSTRAINT uq_embedding_models_profile UNIQUE (profile_name),
    CONSTRAINT ck_embedding_models_profile CHECK (btrim(profile_name) <> ''),
    CONSTRAINT ck_embedding_models_name CHECK (btrim(model_name) <> ''),
    CONSTRAINT ck_embedding_models_revision CHECK (btrim(model_revision) <> ''),
    CONSTRAINT ck_embedding_models_dimension CHECK (dimension BETWEEN 1 AND 16000),
    CONSTRAINT ck_embedding_models_config CHECK (jsonb_typeof(encoding_config) = 'object')
);
-- В model_revision задаётся конкретная версия/commit, а не плавающая ссылка latest.

-- 10. Векторы. Без (n): разные профили могут иметь разную размерность.
-- Совпадение размерности с выбранным профилем проверяет триггер ниже.
CREATE TABLE IF NOT EXISTS docsearch.chunk_embeddings (
    chunk_id BIGINT NOT NULL,
    model_id BIGINT NOT NULL,
    embedding public.vector NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_chunk_embeddings PRIMARY KEY (chunk_id, model_id),
    CONSTRAINT fk_chunk_embeddings_chunk FOREIGN KEY (chunk_id)
        REFERENCES docsearch.document_chunks(chunk_id) ON DELETE CASCADE ON UPDATE RESTRICT,
    CONSTRAINT fk_chunk_embeddings_model FOREIGN KEY (model_id)
        REFERENCES docsearch.embedding_models(model_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT ck_chunk_embeddings_nonzero CHECK (public.vector_norm(embedding) > 0)
);
-- Нулевые векторы запрещены, так как планируется косинусный поиск.
-- ANN-индексы добавим после выбора модели/размерности и загрузки корпуса.

-- 11. Проверка и защита профиля модели.
CREATE OR REPLACE FUNCTION docsearch.protect_model_profile()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path = pg_catalog, docsearch, public
AS $function$
BEGIN
    IF ROW(NEW.model_id, NEW.model_name, NEW.model_revision, NEW.dimension, NEW.encoding_config)
       IS DISTINCT FROM
       ROW(OLD.model_id, OLD.model_name, OLD.model_revision, OLD.dimension, OLD.encoding_config) THEN
        RAISE EXCEPTION 'Профиль модели неизменяем: создайте новую запись модели.'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_protect_model_profile ON docsearch.embedding_models;
CREATE TRIGGER trg_protect_model_profile
BEFORE UPDATE ON docsearch.embedding_models
FOR EACH ROW EXECUTE FUNCTION docsearch.protect_model_profile();

CREATE OR REPLACE FUNCTION docsearch.check_embedding_dimension()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path = pg_catalog, docsearch, public
AS $function$
DECLARE
    expected_dimension INTEGER;
BEGIN
    SELECT m.dimension INTO expected_dimension
    FROM docsearch.embedding_models m WHERE m.model_id = NEW.model_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Модель с model_id=% не существует.', NEW.model_id
            USING ERRCODE = '23503';
    END IF;
    IF public.vector_dims(NEW.embedding) <> expected_dimension THEN
        RAISE EXCEPTION 'Размерность вектора %, а для модели % требуется %.',
            public.vector_dims(NEW.embedding), NEW.model_id, expected_dimension
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_check_embedding_dimension ON docsearch.chunk_embeddings;
CREATE TRIGGER trg_check_embedding_dimension
BEFORE INSERT OR UPDATE ON docsearch.chunk_embeddings
FOR EACH ROW EXECUTE FUNCTION docsearch.check_embedding_dimension();

-- 12. Проверка фрагмента по тексту исходного документа.
CREATE OR REPLACE FUNCTION docsearch.check_chunk_source()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path = pg_catalog, docsearch, public
AS $function$
DECLARE
    source_text TEXT;
BEGIN
    -- Блокируем документ, чтобы параллельная замена текста не создала старые фрагменты.
    SELECT d.extracted_text INTO source_text
    FROM docsearch.documents d WHERE d.document_id = NEW.document_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Документ с document_id=% не существует.', NEW.document_id
            USING ERRCODE = '23503';
    END IF;
    IF source_text IS NULL OR NEW.end_offset > char_length(source_text)
       OR NEW.content IS DISTINCT FROM substring(source_text FROM NEW.start_offset + 1 FOR NEW.end_offset - NEW.start_offset) THEN
        RAISE EXCEPTION 'Фрагмент не соответствует extracted_text документа или выходит за его границы.'
            USING ERRCODE = '23514';
    END IF;
    -- Изменение существующего фрагмента требует пересчёта всех его векторов.
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.document_id, NEW.content, NEW.start_offset, NEW.end_offset)
           IS DISTINCT FROM ROW(OLD.document_id, OLD.content, OLD.start_offset, OLD.end_offset) THEN
            DELETE FROM docsearch.chunk_embeddings WHERE chunk_id = OLD.chunk_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_check_chunk_source ON docsearch.document_chunks;
CREATE TRIGGER trg_check_chunk_source
BEFORE INSERT OR UPDATE ON docsearch.document_chunks
FOR EACH ROW EXECUTE FUNCTION docsearch.check_chunk_source();

-- 13. При замене текста старые фрагменты и векторы удаляются в той же транзакции.
CREATE OR REPLACE FUNCTION docsearch.prepare_document_update()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path = pg_catalog, docsearch, public
AS $function$
BEGIN
    NEW.updated_at := clock_timestamp();
    IF NEW.extracted_text IS DISTINCT FROM OLD.extracted_text THEN
        DELETE FROM docsearch.document_chunks WHERE document_id = OLD.document_id;
        NEW.processing_status := 'pending';
        NEW.error_message := NULL;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_prepare_document_update ON docsearch.documents;
CREATE TRIGGER trg_prepare_document_update
BEFORE UPDATE ON docsearch.documents
FOR EACH ROW EXECUTE FUNCTION docsearch.prepare_document_update();

-- Готовность всей обработки устанавливает приложение ПОСЛЕ загрузки фрагментов
-- и векторов. CHECK готового текста не доказывает полноту векторов каждой модели.
-- Пока это DDL-структура; безопасный поиск и проверка полноты будут отдельным этапом.

-- 14. Индексы для внешних ключей и обычных фильтров.
-- PK и UNIQUE уже создают свои индексы; их не дублируем.
CREATE INDEX IF NOT EXISTS ix_users_role ON docsearch.users(role_id);
CREATE INDEX IF NOT EXISTS ix_collection_access_collection ON docsearch.collection_access(collection_id);
CREATE INDEX IF NOT EXISTS ix_documents_collection ON docsearch.documents(collection_id);
CREATE INDEX IF NOT EXISTS ix_documents_uploaded_by ON docsearch.documents(uploaded_by);
CREATE INDEX IF NOT EXISTS ix_documents_status ON docsearch.documents(processing_status);
CREATE INDEX IF NOT EXISTS ix_document_categories_category ON docsearch.document_categories(category_id);
CREATE INDEX IF NOT EXISTS ix_chunk_embeddings_model ON docsearch.chunk_embeddings(model_id);

-- 15. Описания объектов, видимые в pgAdmin.
COMMENT ON SCHEMA docsearch IS 'Поисковая система по документам на основе векторного поиска';
COMMENT ON TABLE docsearch.roles IS 'Справочник ролей приложения';
COMMENT ON TABLE docsearch.users IS 'Учётные записи пользователей приложения';
COMMENT ON TABLE docsearch.collections IS 'Коллекции документов и границы доступа';
COMMENT ON TABLE docsearch.collection_access IS 'Доступ пользователей к коллекциям, M:N';
COMMENT ON TABLE docsearch.documents IS 'Исходные файлы, извлечённый текст и состояния обработки';
COMMENT ON TABLE docsearch.categories IS 'Справочник тематических категорий';
COMMENT ON TABLE docsearch.document_categories IS 'Назначение категорий документам, M:N';
COMMENT ON TABLE docsearch.document_chunks IS 'Фрагменты извлечённого текста документов';
COMMENT ON TABLE docsearch.embedding_models IS 'Неизменные профили кодирования текстов';
COMMENT ON TABLE docsearch.chunk_embeddings IS 'Векторы фрагментов для разных профилей моделей';
COMMENT ON COLUMN docsearch.documents.original_filename IS 'Имя файла на момент загрузки, включая расширение';
COMMENT ON COLUMN docsearch.documents.storage_key IS 'Адрес исходного файла; сам файл хранится вне БД';
COMMENT ON COLUMN docsearch.users.password_hash IS 'Хеш пароля, созданный приложением; не открытый пароль';
COMMENT ON COLUMN docsearch.document_chunks.start_offset IS 'Начальная позиция с нуля в extracted_text';
COMMENT ON COLUMN docsearch.document_chunks.end_offset IS 'Конечная позиция, не включается во фрагмент';
COMMENT ON COLUMN docsearch.chunk_embeddings.embedding IS 'Размерность проверяется по embedding_models.dimension';
