"""
Перевод описаний модов на язык интерфейса (без Qt).

Описания и списки изменений на Modrinth и CurseForge почти всегда по-английски. Перевод — по кнопке «Перевести»
(или сам, если включено в настройках) через Google Переводчик. Разметка Markdown сохраняется: блоки кода не
переводятся, а ссылки, картинки, код в строке и HTML-теги заменяются метками ⟦n⟧ и возвращаются после перевода.
Переводы запоминаются (JSON в папке кэша), повторный показ — без сети.
"""
import hashlib
import json
import os
import re
import threading
from typing import Dict, List, Optional, Tuple

import config as app_config

ENDPOINT = "https://translate.googleapis.com/translate_a/single"
CHUNK = 4000            # Символов в одном запросе
CACHE_LIMIT = 3000      # Переводов в кэше (старые вытесняются)
CACHE_FILE: Optional[str] = None  # None — рядом с кэшем программы (тесты подменяют)
# Что не переводим: адрес ссылки/картинки, код в строке, HTML-теги, голые адреса
_PROTECT = re.compile(r"\]\([^)]*\)|`[^`\n]+`|<[^>\n]+>|https?://[^\s)\]>]+")
_TOKEN = re.compile(r"⟦(\d+)⟧")
_LOCK = threading.Lock()


def _cache_file() -> str:
    return CACHE_FILE or os.path.join(os.path.dirname(app_config.CACHE_FILE), "translations.json")


class _Cache:
    _items: Optional[Dict[str, str]] = None

    @classmethod
    def items(cls) -> Dict[str, str]:
        if cls._items is None or getattr(cls, "_path", None) != _cache_file():
            cls._path = _cache_file()
            try:
                with open(cls._path, encoding="utf-8") as f:
                    cls._items = json.load(f)
            except (OSError, ValueError):
                cls._items = {}
        return cls._items

    @classmethod
    def save(cls):
        with _LOCK:
            items = cls.items()
            for key in list(items)[:-CACHE_LIMIT]:  # Словарь хранит порядок добавления — старые первыми
                items.pop(key, None)
            try:
                os.makedirs(os.path.dirname(cls._path), exist_ok=True)
                with open(cls._path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False)
                os.replace(cls._path + ".tmp", cls._path)
            except OSError:
                pass


def _key(text: str, target: str) -> str:
    return target + ":" + hashlib.sha1(text.encode("utf-8")).hexdigest()


def _request(text: str, target: str, session=None) -> Tuple[str, str]:
    """(перевод, язык оригинала) одного куска текста."""
    from mod_updater_logic import SESSION
    response = (session or SESSION).post(ENDPOINT, params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t"},
                                         data={"q": text}, timeout=20)
    response.raise_for_status()
    data = response.json()
    return "".join(part[0] for part in data[0] or [] if part and part[0]), (data[2] if len(data) > 2 else "") or ""


def _chunks(lines: List[str]) -> List[List[str]]:
    result, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) > CHUNK:
            result.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        result.append(current)
    return result


def _translate_lines(lines: List[str], target: str, session=None) -> List[str]:
    """Перевод строк с сохранением их числа (Google сохраняет переводы строк)."""
    result = []
    for chunk in _chunks(lines):
        translated, _ = _request("\n".join(chunk), target, session)
        parts = translated.split("\n")
        result += parts if len(parts) == len(chunk) else [translated]  # Строки слились — отдаём кусок целиком
    return result


def translate_markdown(text: str, target: str, session=None) -> str:
    """Перевод Markdown: код и адреса не трогаются, разметка сохраняется. Запоминается."""
    text = text or ""
    if not text.strip():
        return text
    key = _key(text, target)
    cached = _Cache.items().get(key)
    if cached is not None:
        return cached
    protected: List[str] = []

    def protect(match):
        protected.append(match.group(0))
        return f"⟦{len(protected) - 1}⟧"

    lines, todo, in_code = text.split("\n"), [], False
    for index, line in enumerate(lines):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code or not re.search(r"[^\W\d_]", line):  # Код и строки без букв (разделители таблиц) — как есть
            continue
        todo.append(index)
    masked = [_PROTECT.sub(protect, lines[i]) for i in todo]
    translated = _translate_lines(masked, target, session) if masked else []
    if len(translated) == len(todo):
        for index, line in zip(todo, translated):
            lines[index] = line
        result = "\n".join(lines)
    else:
        result = "\n".join(translated)
    result = _TOKEN.sub(lambda m: protected[int(m.group(1))] if int(m.group(1)) < len(protected) else m.group(0), result)
    with _LOCK:
        _Cache.items()[key] = result
    _Cache.save()
    return result


def translate_many(texts: List[str], target: str, session=None) -> List[str]:
    """Несколько текстов разом (описание, страница проекта, список изменений) — пустые не переводятся."""
    return [translate_markdown(t, target, session) if t and t.strip() else t for t in texts]
