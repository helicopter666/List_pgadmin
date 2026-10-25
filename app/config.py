from functools import lru_cache
from pathlib import Path
import re
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    database_url: str = 'postgresql+psycopg://postgres@localhost:5432/document_search'
    session_secret: str = Field(min_length=32)
    cookie_secure: bool = False
    storage_dir: Path = ROOT / 'data' / 'uploads'
    model_name: str = 'intfloat/multilingual-e5-small'
    model_revision: str = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
    max_upload_mb: int = Field(default=10, ge=1, le=50)
    max_text_chars: int = Field(default=400000, ge=1000, le=2000000)
    max_chunks: int = Field(default=3000, ge=1, le=10000)
    @field_validator('model_revision')
    @classmethod
    def pinned_revision(cls, v):
        if not re.fullmatch('[a-f0-9]{40}', v):
            raise ValueError('MODEL_REVISION должен быть полным SHA commit модели')
        return v

@lru_cache
def settings():
    return Settings()
