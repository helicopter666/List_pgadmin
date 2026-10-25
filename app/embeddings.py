import hashlib
import json
import math
from threading import RLock
from sqlalchemy import text
from .config import settings

class Encoder:
    def __init__(self):
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(2)
        cfg = settings()
        self.model = SentenceTransformer(cfg.model_name, revision=cfg.model_revision,
                                         trust_remote_code=False, device='cpu')
        self.lock = RLock()
        self.dimension = int(self.model.get_sentence_embedding_dimension())
        self.max_tokens = min(512, self.model.max_seq_length)
        self.model_name, self.revision = cfg.model_name, cfg.model_revision
        self.config = {'query_prefix': 'query: ', 'document_prefix': 'passage: ',
                       'normalize_embeddings': True, 'max_tokens': self.max_tokens}
        encoded = json.dumps([self.model_name, self.revision, self.config], sort_keys=True)
        self.profile = 'e5-' + hashlib.sha256(encoded.encode()).hexdigest()

    def token_count(self, value):
        with self.lock:
            return len(self.model.tokenizer.encode(value, add_special_tokens=True, truncation=False))

    def passages(self, values):
        with self.lock:
            return self.model.encode(['passage: ' + v for v in values], batch_size=8,
                                     normalize_embeddings=True, show_progress_bar=False).tolist()

    def query(self, value):
        if self.token_count('query: ' + value) > self.max_tokens:
            raise ValueError('Запрос слишком длинный для модели. Сократите его.')
        with self.lock:
            return self.model.encode('query: ' + value, normalize_embeddings=True).tolist()

_encoder = None
_encoder_lock = RLock()

def get_encoder():
    global _encoder
    # Два одновременных первых запроса не должны загрузить две копии модели.
    with _encoder_lock:
        if _encoder is None:
            _encoder = Encoder()
        return _encoder

def ensure_model(conn, encoder):
    conn.execute(text('''INSERT INTO docsearch.embedding_models
        (profile_name, model_name, model_revision, dimension, encoding_config)
        VALUES (:profile,:name,:revision,:dimension,CAST(:config AS jsonb))
        ON CONFLICT(profile_name) DO NOTHING'''),
        {'profile': encoder.profile, 'name': encoder.model_name, 'revision': encoder.revision,
         'dimension': encoder.dimension, 'config': json.dumps(encoder.config)})
    model = conn.execute(text('SELECT * FROM docsearch.embedding_models WHERE profile_name=:p'),
                         {'p': encoder.profile}).mappings().one()
    if (model['model_name'] != encoder.model_name or model['model_revision'] != encoder.revision
            or model['dimension'] != encoder.dimension or model['encoding_config'] != encoder.config):
        raise ValueError('Настройки модели не совпадают с сохранённым профилем.')
    return model['model_id']

def vector_literal(values):
    if not values or any(not math.isfinite(float(v)) for v in values):
        raise ValueError('Модель вернула некорректный вектор.')
    return '[' + ','.join(format(float(v), '.9g') for v in values) + ']'
