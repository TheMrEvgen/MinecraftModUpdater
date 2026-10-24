"""
CurseForge API (без Qt): второй источник модов после Modrinth.

Мод узнаётся по отпечатку файла (MurmurHash2 от содержимого без пробельных символов) — как в официальном
приложении CurseForge. Ключ API берётся из переменной окружения CURSEFORGE_API_KEY или из _build_secrets.py
(его создаёт build.py из секрета GitHub; в git файла нет). Без ключа CurseForge просто не используется.

Авторы могут запретить скачивание сторонними программами (allowModDistribution = false, downloadUrl = null):
такие моды программа находит и показывает, а скачать предлагает со страницы мода в браузере.
"""
import logging
import os
import struct
from typing import Dict, Iterable, List, Optional

from mod_updater_logic import SESSION

API = "https://api.curseforge.com/v1"
GAME_MINECRAFT = 432
CLASS_MODS = 6
# Тип загрузчика в CurseForge
LOADER_TYPES = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
# Тип файла: 1 — релиз, 2 — бета, 3 — альфа
RELEASE, BETA, ALPHA = 1, 2, 3
RELATION_REQUIRED = 3
HASH_SHA1 = 1
ID_PREFIX = "cf:"  # project_id мода с CurseForge — «cf:238222», чтобы не путать с id Modrinth
_WHITESPACE = b"\t\n\r "


def api_key() -> str:
    if "CURSEFORGE_API_KEY" in os.environ:  # Задано явно (пустое значение выключает CurseForge — так в тестах)
        return os.environ["CURSEFORGE_API_KEY"]
    try:
        from _build_secrets import CURSEFORGE_API_KEY  # type: ignore
        return CURSEFORGE_API_KEY or ""
    except ImportError:
        return ""


def available() -> bool:
    return bool(api_key())


def fingerprint(data: bytes) -> int:
    """Отпечаток CurseForge: MurmurHash2 (seed 1) от байтов файла без табуляций, переводов строк и пробелов."""
    data = data.translate(None, _WHITESPACE)
    m, mask = 0x5BD1E995, 0xFFFFFFFF
    length = len(data)
    h = (1 ^ length) & mask
    body = length - length % 4
    for (k,) in struct.iter_unpack("<I", memoryview(data)[:body]):
        k = (k * m) & mask
        k ^= k >> 24
        k = (k * m) & mask
        h = ((h * m) & mask) ^ k
    tail = data[body:]
    if len(tail) == 3:
        h ^= tail[2] << 16
    if len(tail) >= 2:
        h ^= tail[1] << 8
    if tail:
        h ^= tail[0]
        h = (h * m) & mask
    h ^= h >> 13
    h = (h * m) & mask
    h ^= h >> 15
    return h


def file_fingerprint(path: str) -> int:
    with open(path, "rb") as f:
        return fingerprint(f.read())


def project_id(mod_id) -> str:
    return f"{ID_PREFIX}{mod_id}"


def mod_id(project: str) -> Optional[int]:
    return int(project[len(ID_PREFIX):]) if project and project.startswith(ID_PREFIX) else None


def is_curseforge(project: Optional[str]) -> bool:
    return bool(project) and project.startswith(ID_PREFIX)


# --- Запросы ---

def _headers() -> Dict[str, str]:
    return {"x-api-key": api_key(), "Accept": "application/json"}


def _get(path: str, params=None, timeout: int = 20):
    response = SESSION.get(f"{API}{path}", params=params, headers=_headers(), timeout=timeout)
    response.raise_for_status()
    return response.json()


def _post(path: str, payload, timeout: int = 30):
    response = SESSION.post(f"{API}{path}", json=payload, headers=_headers(), timeout=timeout)
    response.raise_for_status()
    return response.json()


def match_fingerprints(fingerprints: Iterable[int]) -> Dict[int, Dict]:
    """Отпечаток → {"file": установленный файл, "mod_id": …}. Только точные совпадения."""
    prints = sorted(set(fingerprints))
    if not prints:
        return {}
    data = _post(f"/fingerprints/{GAME_MINECRAFT}", {"fingerprints": prints}).get("data") or {}
    result = {}
    for match in data.get("exactMatches") or []:
        file = match.get("file") or {}
        fp = file.get("fileFingerprint")
        if fp is not None:
            result[int(fp)] = {"file": file, "mod_id": match.get("id") or file.get("modId")}
    return result


def get_mods(mod_ids: Iterable[int]) -> Dict[int, Dict]:
    ids = sorted({int(i) for i in mod_ids})
    if not ids:
        return {}
    data = _post("/mods", {"modIds": ids, "filterPcOnly": True}).get("data") or []
    return {int(mod["id"]): mod for mod in data}


def get_files(mod: int, game_version: str, loader: str, page_size: int = 50) -> List[Dict]:
    """Файлы мода для версии игры и загрузчика, новые первыми."""
    params = {"gameVersion": game_version, "pageSize": page_size}
    if loader in LOADER_TYPES:
        params["modLoaderType"] = LOADER_TYPES[loader]
    files = _get(f"/mods/{mod}/files", params).get("data") or []
    return sorted(files, key=lambda f: f.get("fileDate") or "", reverse=True)


def files_for(mod: int, game_version: str, loader: Optional[str]) -> List[Dict]:
    """Файлы мода для сборки. Quilt запускает и моды Fabric — спрашиваем оба загрузчика."""
    if loader == "quilt":
        merged = {f["id"]: f for name in ("fabric", "quilt") for f in get_files(mod, game_version, name)}
        return sorted(merged.values(), key=lambda f: f.get("fileDate") or "", reverse=True)
    return get_files(mod, game_version, loader if loader in LOADER_TYPES else None)


def search(query: str, game_version: Optional[str] = None, loader: Optional[str] = None, page_size: int = 30,
           index: int = 0, sort_field: int = 2) -> List[Dict]:
    """Поиск модов. sort_field: 2 — популярность, 3 — дата обновления, 6 — загрузки."""
    params = {"gameId": GAME_MINECRAFT, "classId": CLASS_MODS, "searchFilter": query, "pageSize": page_size,
              "index": index, "sortField": sort_field, "sortOrder": "desc"}
    if game_version:
        params["gameVersion"] = game_version
    if loader in LOADER_TYPES:
        params["modLoaderType"] = LOADER_TYPES[loader]
    return _get("/mods/search", params).get("data") or []


# --- Разбор ответов ---

def file_sha1(file: Dict) -> Optional[str]:
    return next((h.get("value") for h in file.get("hashes") or [] if h.get("algo") == HASH_SHA1), None)


def allowed_types(channel_value: str) -> set:
    """Канал обновлений программы → типы файлов CurseForge."""
    return {"release": {RELEASE}, "beta": {RELEASE, BETA}}.get(channel_value, {RELEASE, BETA, ALPHA})


def fits(file: Dict, game_version: str, loader: str) -> bool:
    """Файл подходит к сборке: та же версия игры и загрузчик (у мультизагрузочных — один из)."""
    versions = {v.lower() for v in file.get("gameVersions") or []}
    if game_version.lower() not in versions:
        return False
    loader_names = {"forge", "fabric", "quilt", "neoforge"} & versions
    return not loader_names or loader in loader_names or (loader == "quilt" and "fabric" in loader_names)


def latest_file(files: List[Dict], game_version: str, loader: str, channel_value: str) -> Optional[Dict]:
    types = allowed_types(channel_value)
    for file in files:  # Уже отсортированы — новые первыми
        if file.get("releaseType") in types and fits(file, game_version, loader) and file.get("isAvailable", True):
            return file
    return None


def page_url(mod: Optional[Dict]) -> str:
    return ((mod or {}).get("links") or {}).get("websiteUrl") or ""




# --- Перевод в формат Modrinth: остальная программа работает с модами CurseForge как с обычными ---

RELEASE_TYPES = {RELEASE: "release", BETA: "beta", ALPHA: "alpha"}
# relationType: 1 — встроена, 2 — необязательная, 3 — обязательная, 4 — инструмент, 5 — несовместима, 6 — включена
RELATIONS = {1: "embedded", 2: "optional", 3: "required", 4: "optional", 5: "incompatible", 6: "embedded"}


def file_page(mod: Optional[Dict], file: Dict) -> str:
    base = page_url(mod)
    return f"{base}/files/{file.get('id')}" if base and file.get("id") else base


def to_version(file: Dict, mod: Optional[Dict] = None) -> Dict:
    """Файл CurseForge как версия Modrinth. Если автор запретил скачивание, url = None, а manual_url — страница файла."""
    names = [v for v in file.get("gameVersions") or []]
    url = file.get("downloadUrl") if (mod or {}).get("allowModDistribution", True) is not False else None
    return {
        "id": f"cf-file:{file.get('id')}", "project_id": project_id(file.get("modId")),
        "version_number": file.get("displayName") or file.get("fileName") or "",
        "version_type": RELEASE_TYPES.get(file.get("releaseType"), "release"),
        "date_published": file.get("fileDate"),
        "game_versions": [v for v in names if v[:1].isdigit()],
        "loaders": [v.lower() for v in names if v.lower() in LOADER_TYPES],
        "changelog": "",
        "files": [{"url": url, "filename": file.get("fileName"), "primary": True, "size": file.get("fileLength") or 0,
                   "hashes": {"sha1": file_sha1(file)} if file_sha1(file) else {}}],
        "dependencies": [{"project_id": project_id(d.get("modId")), "dependency_type": RELATIONS.get(d.get("relationType"), "optional")}
                         for d in file.get("dependencies") or [] if d.get("modId")],
        "manual_url": "" if url else file_page(mod, file),
        "cf_file_id": file.get("id"),
    }


def to_project(mod: Dict) -> Dict:
    """Мод CurseForge как проект Modrinth (название, иконка, ссылки, галерея, загрузчики)."""
    links = mod.get("links") or {}
    loaders = sorted({name for index in mod.get("latestFilesIndexes") or []
                      for name, code in LOADER_TYPES.items() if index.get("modLoader") == code})
    return {
        "id": project_id(mod.get("id")), "slug": mod.get("slug"), "title": mod.get("name"),
        "description": mod.get("summary") or "", "body": mod.get("summary") or "",
        "icon_url": (mod.get("logo") or {}).get("thumbnailUrl"), "client_side": None, "server_side": None,
        "categories": [c.get("name") for c in mod.get("categories") or [] if c.get("name")],
        "project_type": "mod", "updated": mod.get("dateModified"), "downloads": mod.get("downloadCount"),
        "loaders": loaders, "source_url": links.get("sourceUrl"), "issues_url": links.get("issuesUrl"),
        "wiki_url": links.get("wikiUrl"), "page_url": page_url(mod), "source": "curseforge",
        "gallery": [{"url": s.get("url"), "raw_url": s.get("url"), "title": s.get("title"), "featured": False}
                    for s in mod.get("screenshots") or [] if s.get("url")],
        "allow_distribution": mod.get("allowModDistribution", True) is not False,
    }


def get_project(project: str) -> Dict:
    """Полная информация о моде (с описанием) в формате Modrinth."""
    mod = _get(f"/mods/{mod_id(project)}").get("data") or {}
    result = to_project(mod)
    try:
        result["body"] = html_to_markdown(_get(f"/mods/{mod_id(project)}/description").get("data") or "") or result["body"]
    except Exception as e:  # Описание не обязательно — показываем краткое
        logging.debug(f"CurseForge: описание {project} не загружено: {e}")
    return result


def get_versions(project: str, game_version: str, loader) -> List[Dict]:
    """Версии мода для версии игры и загрузчика в формате Modrinth (новые первыми)."""
    loader_name = loader[0] if isinstance(loader, (list, tuple)) and loader else loader
    loader_name = loader_name if loader_name in LOADER_TYPES else ""  # Шейдеры и ресурспаки — без загрузчика
    mods = get_mods([mod_id(project)])
    mod = mods.get(mod_id(project))
    files = [f for f in files_for(mod_id(project), game_version, loader_name or None)
             if fits(f, game_version, loader_name)]
    return [to_version(f, mod) for f in files]


def get_projects(ids: Iterable[str]) -> Dict[str, Dict]:
    mods = get_mods(i for i in (mod_id(p) for p in ids) if i is not None)
    return {project_id(m_id): to_project(mod) for m_id, mod in mods.items()}


def file_changelog(project: str, file_id) -> str:
    try:
        html = _get(f"/mods/{mod_id(project)}/files/{file_id}/changelog").get("data") or ""
    except Exception as e:
        logging.debug(f"CurseForge: список изменений {project}/{file_id} не загружен: {e}")
        return ""
    return html_to_markdown(html)


def html_to_markdown(html: str) -> str:
    """Описания и списки изменений CurseForge приходят в HTML — переводим в простой Markdown."""
    import html as html_lib
    import re
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n\n" + "#" * min(6, int(m.group(1)) + 2) + " ", text)
    text = re.sub(r"(?i)</li>", "", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|ul|ol|tr)>", "\n", text)
    text = re.sub(r"(?i)<(strong|b)>(.*?)</\1>", r"**\2**", text)
    text = re.sub(r"(?is)<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", lambda m: f"[{m.group(2)}]({_real_link(m.group(1))})", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)  # Ссылки-картинки: картинок нет — пустую ссылку убираем
    text = html_lib.unescape(text)
    text = re.sub(r"(?m)^#+\s*$", "", text)  # Заголовок, в котором была только картинка
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.strip().splitlines()))


# --- Каталог ---

CLASS_IDS = {"mods": CLASS_MODS, "shaders": 6552, "resourcepacks": 12, "datapacks": 6945}
# Сортировка каталога программы → sortField CurseForge (2 — популярность, 3 — обновление, 6 — загрузки, 11 — выход, 12 — оценка)
SORT_FIELDS = {"relevance": 2, "downloads": 6, "updated": 3, "newest": 11, "follows": 12}
MAX_RESULTS = 10_000  # CurseForge не отдаёт результаты дальше 10 000-го


def to_hit(mod: Dict, project_type: str = "mod") -> Dict:
    """Мод CurseForge как результат поиска Modrinth."""
    categories = [c.get("name") for c in mod.get("categories") or [] if c.get("name")]
    return {
        "project_id": project_id(mod.get("id")), "slug": mod.get("slug"), "title": mod.get("name"),
        "description": mod.get("summary") or "", "icon_url": (mod.get("logo") or {}).get("thumbnailUrl"),
        "downloads": mod.get("downloadCount") or 0, "follows": mod.get("thumbsUpCount") or 0,
        "date_modified": mod.get("dateModified"), "author": ", ".join(a.get("name", "") for a in (mod.get("authors") or [])[:2]),
        "categories": categories, "display_categories": categories, "project_type": project_type,
        "client_side": None, "server_side": None, "license": None, "page_url": page_url(mod), "source": "curseforge",
        "allow_distribution": mod.get("allowModDistribution", True) is not False,
    }


def search_hits(query: str, content_key: str = "mods", game_version: Optional[str] = None,
                loader: Optional[str] = None, sort: str = "relevance", offset: int = 0, limit: int = 20,
                category_id: Optional[int] = None) -> Dict:
    """Поиск для каталога: {"hits": [...], "total_hits": N} в формате Modrinth."""
    params = {"gameId": GAME_MINECRAFT, "classId": CLASS_IDS.get(content_key, CLASS_MODS), "searchFilter": query or "",
              "sortField": SORT_FIELDS.get(sort, 2), "sortOrder": "desc", "index": max(0, offset),
              "pageSize": max(1, min(limit, 50))}
    if game_version:
        params["gameVersion"] = game_version
    if loader in LOADER_TYPES and loader != "quilt" and content_key == "mods":
        params["modLoaderType"] = LOADER_TYPES[loader]
    if category_id:
        params["categoryId"] = category_id
    data = _get("/mods/search", params)
    project_type = {"mods": "mod", "shaders": "shader", "resourcepacks": "resourcepack",
                    "datapacks": "datapack"}.get(content_key, "mod")
    total = int((data.get("pagination") or {}).get("totalCount") or 0)
    return {"hits": [to_hit(mod, project_type) for mod in data.get("data") or []], "total_hits": min(total, MAX_RESULTS)}


def categories(content_key: str = "mods") -> List[Dict]:
    """Категории раздела: [{"id", "name"}] по алфавиту (только верхнего уровня раздела)."""
    class_id = CLASS_IDS.get(content_key, CLASS_MODS)
    data = _get("/categories", {"gameId": GAME_MINECRAFT, "classId": class_id}).get("data") or []
    return sorted(({"id": c["id"], "name": c["name"]} for c in data if not c.get("isClass")), key=lambda c: c["name"])


def _real_link(href: str) -> str:
    """«/linkout?remoteUrl=…» (переходник CurseForge, адрес закодирован дважды) → настоящий адрес."""
    from urllib.parse import parse_qs, unquote, urlparse
    if "linkout" in href and "remoteUrl=" in href:
        remote = parse_qs(urlparse(href).query).get("remoteUrl", [""])[0]
        return unquote(remote) or href
    if href.startswith("/"):
        return "https://www.curseforge.com" + href
    return href


def get_files_by_ids(file_ids: Iterable[int]) -> Dict[int, Dict]:
    """Файлы по номерам (сборки CurseForge перечисляют моды как projectID + fileID)."""
    ids = sorted({int(i) for i in file_ids})
    result: Dict[int, Dict] = {}
    for start in range(0, len(ids), 500):
        for file in _post("/mods/files", {"fileIds": ids[start:start + 500]}).get("data") or []:
            result[int(file["id"])] = file
    return result
