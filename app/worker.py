"""Один устойчивый worker: очередь — pending документы, блокировка — PostgreSQL."""
import logging
import time
from sqlalchemy import text
from .db import engine
from .documents import extract, make_chunks, stored_file
from .embeddings import get_encoder, ensure_model, vector_literal

log = logging.getLogger(__name__)
LOCK_ID = 843921004

def process_one(encoder=None):
    with engine().begin() as c:
        document = c.execute(text('''SELECT * FROM docsearch.documents
            WHERE processing_status='pending' ORDER BY document_id
            LIMIT 1 FOR UPDATE SKIP LOCKED''')).mappings().first()
        if not document:
            return False
        document = dict(document)
        c.execute(text("UPDATE docsearch.documents SET processing_status='processing', error_message=NULL WHERE document_id=:id"), {'id': document['document_id']})
    did = document['document_id']
    try:
        extracted, pages = extract(stored_file(document['storage_key']), document['file_format'])
        encoder = encoder or get_encoder()
        chunks = make_chunks(extracted, encoder.token_count, encoder.max_tokens, pages)
        vectors = encoder.passages([c.content for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError('Количество векторов не соответствует количеству фрагментов.')
        with engine().begin() as c:
            current = c.execute(text('SELECT * FROM docsearch.documents WHERE document_id=:id FOR UPDATE'), {'id': did}).mappings().first()
            if not current or current['processing_status'] != 'processing':
                return True
            mid = ensure_model(c, encoder)
            c.execute(text('UPDATE docsearch.documents SET extracted_text=:t WHERE document_id=:id'), {'t': extracted, 'id': did})
            # Повторная обработка того же текста тоже пересоздаёт прежние фрагменты.
            c.execute(text('DELETE FROM docsearch.document_chunks WHERE document_id=:id'), {'id': did})
            for chunk, vector in zip(chunks, vectors):
                cid = c.execute(text('''INSERT INTO docsearch.document_chunks
                    (document_id,chunk_number,content,start_offset,end_offset,page_number)
                    VALUES (:did,:n,:content,:start,:end,:page) RETURNING chunk_id'''),
                    {'did': did, 'n': chunk.number, 'content': chunk.content, 'start': chunk.start,
                     'end': chunk.end, 'page': chunk.page}).scalar_one()
                c.execute(text('''INSERT INTO docsearch.chunk_embeddings(chunk_id,model_id,embedding)
                    VALUES (:cid,:mid,CAST(:v AS public.vector))'''),
                    {'cid': cid, 'mid': mid, 'v': vector_literal(vector)})
            c.execute(text("UPDATE docsearch.documents SET processing_status='ready',error_message=NULL WHERE document_id=:id"), {'id': did})
        log.info('Document %s indexed: %s chunks', did, len(chunks))
    except Exception as exc:
        log.exception('Processing document %s failed', did)
        # Читателю не раскрываем технические детали; редактор увидит краткую причину.
        message = str(exc)[:800] if isinstance(exc, (ValueError, FileNotFoundError)) else 'Не удалось обработать файл или загрузить модель. Подробности: журнал worker.'
        message = message or 'Не удалось обработать документ.'
        with engine().begin() as c:
            c.execute(text("UPDATE docsearch.documents SET processing_status='error',error_message=:m WHERE document_id=:id"), {'m': message, 'id': did})
    return True

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Session lock: другой worker откажется от запуска; после падения lock освободится.
    with engine().connect() as lock_conn:
        acquired = lock_conn.execute(text('SELECT pg_try_advisory_lock(:id)'), {'id': LOCK_ID}).scalar_one()
        lock_conn.commit()
        if not acquired:
            raise SystemExit('Worker уже запущен для этой базы данных.')
        try:
            with engine().begin() as c:
                c.execute(text("UPDATE docsearch.documents SET processing_status='pending',error_message=NULL WHERE processing_status='processing'"))
            while True:
                # Проверка соединения-владельца блокировки; при его потере worker завершается.
                lock_conn.execute(text('SELECT 1')); lock_conn.commit()
                if not process_one():
                    time.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                lock_conn.execute(text('SELECT pg_advisory_unlock(:id)'), {'id': LOCK_ID})
                lock_conn.commit()
            except Exception:
                pass

if __name__ == '__main__':
    main()
