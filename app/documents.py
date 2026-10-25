from bisect import bisect_right
from pathlib import Path
from dataclasses import dataclass
from zipfile import ZipFile, BadZipFile
from .config import settings

@dataclass(frozen=True)
class Chunk:
    number: int
    content: str
    start: int
    end: int
    page: int | None

def stored_file(key):
    base = settings().storage_dir.resolve()
    result = (base / key).resolve()
    if not result.is_relative_to(base) or result == base:
        raise ValueError('Недопустимый адрес исходного файла.')
    return result

def normalize(text):
    return text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')

def extract(path: Path, extension: str):
    """Возвращает текст и начала страниц PDF. DOCX не имеет стабильной пагинации."""
    starts = None
    if extension == 'txt':
        raw = path.read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text = raw.decode('cp1251')
            except UnicodeDecodeError as exc:
                raise ValueError('Сохраните TXT в кодировке UTF-8.') from exc
    elif extension == 'docx':
        from docx import Document
        from docx.text.paragraph import Paragraph
        from docx.table import Table
        try:
            with ZipFile(path) as archive:
                if sum(i.file_size for i in archive.infolist()) > 30 * 1024**2:
                    raise ValueError('Распакованный DOCX слишком большой.')
            doc = Document(path)
        except (BadZipFile, KeyError) as exc:
            raise ValueError('Не удалось прочитать DOCX.') from exc
        parts = []
        for child in doc.element.body:
            if child.tag.endswith('}p'):
                parts.append(Paragraph(child, doc).text)
            elif child.tag.endswith('}tbl'):
                parts.extend(' | '.join(c.text for c in r.cells) for r in Table(child, doc).rows)
        text = '\n\n'.join(parts)
    elif extension == 'pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('Зашифрованный PDF: сначала сохраните копию без пароля.')
        if len(reader.pages) > 500:
            raise ValueError('В первой версии допускается до 500 страниц PDF.')
        parts, starts, offset = [], [], 0
        for page in reader.pages:
            starts.append(offset)
            value = normalize(page.extract_text() or '')
            parts.append(value)
            offset += len(value) + 2
            if offset > settings().max_text_chars:
                raise ValueError('Извлечённый текст превышает допустимый объём.')
        text = '\n\n'.join(parts)
    else:
        raise ValueError('Поддерживаются TXT, DOCX и PDF с текстовым слоем.')
    text = normalize(text)
    if not text.strip():
        raise ValueError('В файле нет извлекаемого текста. Распознавание сканов пока не поддерживается.')
    if len(text) > settings().max_text_chars:
        raise ValueError('Извлечённый текст превышает допустимый объём.')
    return text, starts

def make_chunks(text, token_count, max_tokens, page_starts=None, target_chars=1400, overlap=140):
    """Срезы исходной строки: offsets совпадают с PostgreSQL char_length/substring."""
    if target_chars <= overlap or overlap < 0 or max_tokens < 8:
        raise ValueError('Неверные параметры разбиения.')
    start, result = 0, []
    while start < len(text):
        end = min(start + target_chars, len(text))
        # Учитываем префикс passage и служебные токены: модель не обрезает текст молча.
        while token_count('passage: ' + text[start:end]) > max_tokens:
            if end - start <= 1:
                raise ValueError('Фрагмент не помещается в контекст модели.')
            end = start + max(1, int((end - start) * .75))
        if end < len(text):
            cut = max(text.rfind('\n', start + (end-start)//2, end),
                      text.rfind(' ', start + (end-start)//2, end))
            if cut > start:
                end = cut
        content = text[start:end]
        if content.strip():
            page = bisect_right(page_starts, start) if page_starts is not None else None
            result.append(Chunk(len(result)+1, content, start, end, page))
        if len(result) > settings().max_chunks:
            raise ValueError('Получилось слишком много фрагментов.')
        if end >= len(text):
            break
        start = max(start + 1, end - min(overlap, (end-start)//4))
    if not result:
        raise ValueError('Не удалось получить непустые фрагменты.')
    return result
