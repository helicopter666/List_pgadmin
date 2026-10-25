from sqlalchemy import text
from .db import engine
from .embeddings import get_encoder, vector_literal

# Один и тот же фильтр доступа применяется ДО ранжирования во всех режимах.
BASE = '''FROM docsearch.document_chunks ch
JOIN docsearch.documents d ON d.document_id=ch.document_id
WHERE d.processing_status='ready'
AND (:admin OR EXISTS (SELECT 1 FROM docsearch.collections c
                      WHERE c.collection_id=d.collection_id AND c.access_level='registered')
     OR EXISTS (SELECT 1 FROM docsearch.collection_access ac
                      WHERE ac.collection_id=d.collection_id AND ac.user_id=:uid))
AND (:collection=0 OR d.collection_id=:collection)
AND (:category=0 OR EXISTS (SELECT 1 FROM docsearch.document_categories dc
                           WHERE dc.document_id=d.document_id AND dc.category_id=:category))'''
FIELDS = 'ch.chunk_id, ch.document_id, ch.chunk_number, ch.content, ch.page_number, d.title'

def search(user, query, mode='vector', k=10, collection=0, category=0, encoder=None):
    query = query.strip()
    if not query or len(query) > 2000:
        raise ValueError('Введите запрос длиной от 1 до 2000 символов.')
    if mode not in ('vector', 'lexical', 'hybrid') or not 1 <= k <= 30:
        raise ValueError('Неверные параметры поиска.')
    params = {'admin': user['role'] == 'admin', 'uid': user['user_id'], 'q': query,
              'collection': collection, 'category': category, 'limit': k if mode != 'hybrid' else 100}
    encoder = (encoder or get_encoder()) if mode != 'lexical' else None
    if encoder:
        params['vector'] = vector_literal(encoder.query(query))
        params['profile'] = encoder.profile
    with engine().connect() as c:
        lexical, vectors = [], []
        if mode in ('lexical', 'hybrid'):
            lexical = [dict(r) for r in c.execute(text('SELECT ' + FIELDS + ", ts_rank_cd(to_tsvector('russian',ch.content), websearch_to_tsquery('russian',:q)) AS score " + BASE + " AND to_tsvector('russian',ch.content) @@ websearch_to_tsquery('russian',:q) ORDER BY score DESC,ch.chunk_id LIMIT :limit"), params).mappings()]
        if encoder:
            model = c.execute(text('SELECT model_id FROM docsearch.embedding_models WHERE profile_name=:profile'), params).scalar_one_or_none()
            if model is None:
                if mode == 'hybrid':
                    return [dict(item, score=1/(60+rank)) for rank,item in enumerate(lexical[:k],1)]
                return []
            params['mid'] = model
            # Только полностью проиндексированные выбранной моделью документы.
            vector_sql = ('SELECT ' + FIELDS + ', 1 - (ce.embedding <=> CAST(:vector AS public.vector)) AS score '
                + BASE.replace('WHERE d.processing_status', 'JOIN docsearch.chunk_embeddings ce ON ce.chunk_id=ch.chunk_id\nWHERE d.processing_status')
                + ''' AND ce.model_id=:mid
                AND NOT EXISTS (
                  SELECT 1 FROM docsearch.document_chunks other
                  WHERE other.document_id=d.document_id AND NOT EXISTS (
                    SELECT 1 FROM docsearch.chunk_embeddings oe
                    WHERE oe.chunk_id=other.chunk_id AND oe.model_id=:mid))
                ORDER BY ce.embedding <=> CAST(:vector AS public.vector),ch.chunk_id LIMIT :limit''')
            vectors = [dict(r) for r in c.execute(text(vector_sql), params).mappings()]
    if mode == 'lexical':
        return lexical
    if mode == 'vector':
        return vectors
    merged = {}
    for items in (lexical, vectors):
        for rank, item in enumerate(items, 1):
            saved = merged.setdefault(item['chunk_id'], dict(item, score=0.0))
            saved['score'] += 1 / (60 + rank)
    return sorted(merged.values(), key=lambda x: (-x['score'], x['chunk_id']))[:k]
