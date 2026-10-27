"""
GitHub Releases — третий источник модов после Modrinth и CurseForge (без Qt).

Многие моды выходят только на GitHub. Мод, которого нет в каталогах, связывается с репозиторием:
  * сам — по ссылке на исходники в метаданных jar (fabric.mod.json, quilt.mod.json, mods.toml), но только
    если установленный файл действительно есть среди выпусков (тот же файл по SHA-256, то же имя или версия);
  * вручную — пользователь один раз вставляет ссылку на репозиторий (links: ключ мода -> «владелец/репо»).
Дальше мод обновляется как остальные: выпуски переводятся в формат версий Modrinth.

Без ключа GitHub даёт 60 запросов в час, поэтому ответы запоминаются (ETag + 6 часов), а ответ «не изменилось»
лимит не тратит. Ключ можно задать переменной окружения GITHUB_TOKEN.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
import zipfile
from typing import Dict, List, Optional, Tuple

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

import config as app_config
from content import enabled_name
from i18n import tr

API = "https://api.github.com"
PREFIX = "gh:"  # project_id мода с GitHub — «gh:владелец/репо»
# None — рядом с настройками и кэшем программы (пути берутся в момент обращения: тесты их подменяют)
LINKS_FILE: Optional[str] = None
CACHE_FILE: Optional[str] = None
FRESH_SECONDS = 6 * 3600
LOADERS = ("neoforge", "forge", "fabric", "quilt")
SKIP_SUFFIXES = ("-sources.jar", "-dev.jar", "-javadoc.jar", "-dev-shadow.jar", "-slim.jar")
_REPO = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?]|$)")
# Версия Minecraft в имени файла: 1.7–1.99(.x) или новая нумерация (26.1 и дальше, по году выпуска)
_GAME_VERSION = re.compile(r"(?<![\d.])(1\.(?:[7-9]|[1-9]\d)(?:\.\d{1,2})?|(?:2[6-9]|[3-9]\d)\.\d{1,2}(?:\.\d{1,2})?)(?![\d])")


class RateLimited(Exception):
    pass


# --- Идентификаторы и ссылки ---

def project_id(repo: str) -> str:
    return PREFIX + repo


def is_github(project: Optional[str]) -> bool:
    return str(project or "").startswith(PREFIX)


def repo_of(project: str) -> str:
    return str(project)[len(PREFIX):]


def parse_repo(text: str) -> Optional[str]:
    """«https://github.com/owner/repo/releases» или «owner/repo» -> «owner/repo»."""
    text = (text or "").strip()
    match = _REPO.search(text)
    if match:
        owner, repo = match.groups()
    else:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text)
        if not match:
            return None
        owner, repo = match.groups()
    return None if owner.lower() in ("orgs", "users", "sponsors") else f"{owner}/{repo}"


def page_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def jar_info(path: str) -> Dict[str, str]:
    """Из метаданных jar: id мода, описание и репозиторий из ссылок на исходники/сайт/баг-трекер."""
    info: Dict[str, str] = {}
    urls: List[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "fabric.mod.json" in names:
                data = json.loads(z.read("fabric.mod.json").decode("utf-8", errors="ignore"), strict=False)
                info["id"], info["description"] = str(data.get("id") or ""), str(data.get("description") or "")
                contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
                urls += [str(contact.get(k) or "") for k in ("sources", "homepage", "issues")]
            elif "quilt.mod.json" in names:
                loader = json.loads(z.read("quilt.mod.json").decode("utf-8", errors="ignore"), strict=False).get("quilt_loader") or {}
                metadata = loader.get("metadata") or {}
                info["id"], info["description"] = str(loader.get("id") or ""), str(metadata.get("description") or "")
                contact = metadata.get("contact") if isinstance(metadata.get("contact"), dict) else {}
                urls += [str(contact.get(k) or "") for k in ("sources", "homepage", "issues")]
            else:
                toml_path = next((p for p in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml") if p in names), None)
                if toml_path and tomllib:
                    try:
                        data = tomllib.loads(z.read(toml_path).decode("utf-8", errors="ignore"))
                        mod = (data.get("mods") or [{}])[0]
                        info["id"], info["description"] = str(mod.get("modId") or ""), str(mod.get("description") or "").strip()
                        urls += [str(mod.get("displayURL") or ""), str(data.get("issueTrackerURL") or "")]
                    except Exception:
                        pass
    except (OSError, zipfile.BadZipFile, ValueError, AttributeError):
        return info
    info["repo"] = next((r for r in (parse_repo(u) for u in urls if "github.com" in u) if r), "")
    return info


def link_key(path: str, mod_id: str = "") -> str:
    """Ключ ручной связи: id мода из метаданных, а без него — имя файла без версии (у новой версии оно то же)."""
    if mod_id:
        return "id:" + mod_id.lower()
    stem = re.split(r"[-_+ ]v?\d", enabled_name(os.path.basename(path)).lower())[0]
    return "file:" + re.sub(r"[^a-z0-9]", "", stem)


_LOCK = threading.Lock()


def _links_file() -> str:
    return LINKS_FILE or os.path.join(app_config.CONFIG_DIR, "github_links.json")


def _cache_file() -> str:
    return CACHE_FILE or os.path.join(os.path.dirname(app_config.CACHE_FILE), "github_cache.json")


def load_links() -> Dict[str, str]:
    try:
        with open(_links_file(), encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_link(key: str, repo: Optional[str]):
    """Связывает мод с репозиторием (None — убирает связь, «» — «не с GitHub»: не искать автоматически)."""
    with _LOCK:
        links = load_links()
        if repo is None:
            links.pop(key, None)
        else:
            links[key] = repo
        path = _links_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(links, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)


# --- Запросы с кэшем ---

class _Cache:
    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = {}

    def save(self):
        with _LOCK:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(self.items, f)
                os.replace(self.path + ".tmp", self.path)
            except OSError:
                pass


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return headers


def _get(url: str, cache: _Cache, session=None):
    """GET с запоминанием: свежий ответ — из кэша; иначе запрос с If-None-Match (304 лимит не тратит)."""
    from mod_updater_logic import SESSION
    cached = cache.items.get(url)
    if cached and time.time() - cached.get("time", 0) < FRESH_SECONDS:
        return cached["data"]
    headers = _headers()
    if cached and cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    response = (session or SESSION).get(url, headers=headers, timeout=20)
    if response.status_code == 304 and cached:
        cached["time"] = time.time()
        return cached["data"]
    if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
        if cached:
            return cached["data"]
        raise RateLimited(tr("GitHub: лимит запросов исчерпан, попробуйте через час."))
    if response.status_code == 404:
        data = None
    else:
        response.raise_for_status()
        data = response.json()
    cache.items[url] = {"time": time.time(), "etag": response.headers.get("ETag", ""), "data": data}
    return data


def releases(repo: str, cache: _Cache, session=None) -> List[Dict]:
    """Выпуски репозитория (новые первыми), без черновиков; [] — репозитория нет."""
    data = _get(f"{API}/repos/{repo}/releases?per_page=100", cache, session) or []
    items = [r for r in data if isinstance(r, dict) and not r.get("draft")]
    return sorted(items, key=lambda r: r.get("published_at") or "", reverse=True)


# --- Выбор файла выпуска ---

def _loaders_in(text: str) -> set:
    return set(re.findall(r"[a-z]+", text.lower())) & set(LOADERS)


def _game_versions_in(text: str) -> set:
    return set(_GAME_VERSION.findall(text.lower()))


def asset_fits(name: str, release_text: str, game_version: str, loader: Optional[str],
               installed_name: str = "", installed_versions: frozenset = frozenset()) -> bool:
    """Подходит ли файл выпуска к сборке: загрузчик и версия игры — по имени файла и названию выпуска."""
    lower = name.lower()
    if not lower.endswith(".jar") or lower.endswith(SKIP_SUFFIXES):
        return False
    named = _loaders_in(name)
    if loader and named and not named & ({loader, "fabric"} if loader == "quilt" else {loader}):
        return False
    if not game_version:
        return True
    text = f"{name} {release_text}".lower()
    mentioned = _game_versions_in(text)
    if game_version in mentioned:
        return True
    family = game_version.rsplit(".", 1)[0] if game_version.count(".") >= 2 else game_version
    if re.search(rf"(?<![\d.]){re.escape(family)}\.x", text):  # «для 1.21.x»
        return True
    if mentioned and mentioned <= installed_versions:  # Так же подписан выпуск, который стоит сейчас («для 1.21»)
        return True
    if mentioned:  # Названа другая версия Minecraft — файл для неё
        return False
    # Версий нигде нет: подходит, если и установленный файл версию игры не называл (мод для одной версии)
    return game_version not in _game_versions_in(installed_name)


def _release_text(release: Dict) -> str:
    return f"{release.get('tag_name') or ''} {release.get('name') or ''}"


def best_asset(release: Dict, game_version: str, loader: Optional[str], installed_name: str = "",
               installed_versions: frozenset = frozenset()) -> Optional[Dict]:
    text = _release_text(release)
    fitting = [a for a in release.get("assets") or []
               if asset_fits(a.get("name") or "", text, game_version, loader, installed_name, installed_versions)]
    if not fitting:
        return None
    return min(fitting, key=lambda a: (loader not in _loaders_in(a["name"]) if loader else False,
                                       game_version not in _game_versions_in(a["name"]), len(a["name"])))


def _norm(version: str) -> str:
    return re.sub(r"^(?:v|version|release)[-_ ]?", "", (version or "").strip().lower())


def to_version(repo: str, release: Dict, asset: Dict) -> Dict:
    """Выпуск GitHub как версия Modrinth: остальная программа работает с ним как с обычной версией."""
    hashes = {}
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        hashes["sha256"] = digest.split(":", 1)[1]
    return {
        "id": f"gh-release:{release.get('id')}:{asset.get('id')}", "project_id": project_id(repo),
        "version_number": release.get("tag_name") or release.get("name") or "",
        "name": release.get("name") or release.get("tag_name") or "",
        "version_type": "beta" if release.get("prerelease") else "release",
        "date_published": release.get("published_at"),
        "game_versions": sorted(_game_versions_in(f"{asset.get('name')} {release.get('name') or ''}")),
        "loaders": sorted(_loaders_in(asset.get("name") or "")),
        "changelog": release.get("body") or "",
        "files": [{"url": asset.get("browser_download_url"), "filename": asset.get("name"), "primary": True,
                   "size": asset.get("size") or 0, "hashes": hashes}],
        "dependencies": [],
    }


def versions_for(repo: str, items: List[Dict], game_version: str, loader: Optional[str],
                 installed_name: str = "", installed_versions: frozenset = frozenset()) -> List[Dict]:
    result = []
    for release in items:
        asset = best_asset(release, game_version, loader, installed_name, installed_versions)
        if asset:
            result.append(to_version(repo, release, asset))
    return result


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def find_installed(items: List[Dict], path: str, version: str) -> Optional[Tuple[Dict, Dict]]:
    """(выпуск, файл), из которого установлен мод: по SHA-256, имени файла или номеру версии."""
    name = enabled_name(os.path.basename(path))
    digest = None
    for release in items:
        for asset in release.get("assets") or []:
            if (asset.get("digest") or "").startswith("sha256:"):
                digest = digest or _sha256(path)
                if asset["digest"] == f"sha256:{digest}":
                    return release, asset
            if asset.get("name") == name:
                return release, asset
    wanted = _norm(version)
    if wanted:
        for release in items:
            tag = _norm(release.get("tag_name") or "")
            if tag and (tag == wanted or wanted.split("+")[0] == tag.split("+")[0]):
                assets = [a for a in release.get("assets") or [] if (a.get("name") or "").lower().endswith(".jar")]
                if assets:
                    return release, assets[0]
    return None


# --- Для сканера: записи модов с GitHub ---

def entries_for(locals_: List[Dict], game_version: str, loader: Optional[str], channel_types: Optional[List[str]],
                make_entry, apply_install_info, progress=lambda msg: None, session=None) -> Dict[str, Dict]:
    """
    Моды, которых нет ни на Modrinth, ни на CurseForge: путь -> запись мода (как у Modrinth).
    make_entry и apply_install_info — функции сканера (чтобы не зависеть от него по кругу).
    """
    links = load_links()
    candidates = []
    for local in locals_:
        info = jar_info(local["path"]) if local["path"].lower().endswith((".jar", ".jar.disabled")) else {}
        manual = links.get(link_key(local["path"], info.get("id", "")))
        if manual == "":  # Пользователь сказал «не с GitHub»
            continue
        repo = manual or info.get("repo")
        if repo:
            candidates.append((local, info, repo, bool(manual)))
    if not candidates:
        return {}
    progress(tr("Ищем {0} модов на GitHub…", len(candidates)))
    cache = _Cache(_cache_file())
    result: Dict[str, Dict] = {}
    limited = False
    for local, info, repo, manual in candidates:
        if limited:
            break
        try:
            items = releases(repo, cache, session)
        except RateLimited as e:
            logging.warning(str(e))
            limited = True
            continue
        except Exception as e:
            logging.warning(tr("GitHub: выпуски {0} не получены: {1}", repo, e))
            continue
        allowed = [r for r in items if not channel_types or ("beta" if r.get("prerelease") else "release") in channel_types]
        installed = find_installed(items, local["path"], local.get("version") or "")
        if not installed and not manual:  # Ссылка из метаданных не подтвердилась — это не тот репозиторий
            continue
        name = enabled_name(os.path.basename(local["path"]))
        entry = make_entry(local)
        entry.update({"project_id": project_id(repo), "source": "github", "page_url": page_url(repo),
                      "description": info.get("description") or "", "icon_url": f"https://github.com/{repo.split('/')[0]}.png",
                      "slug": repo.split("/")[1].lower(), "links": [(tr("Исходный код"), "code", page_url(repo))]})
        newer, installed_versions = allowed, frozenset()
        if installed:
            release, asset = installed
            entry["installed_info"] = to_version(repo, release, asset)
            entry["current_version"] = local.get("version") or release.get("tag_name") or entry["current_version"]
            newer = [r for r in allowed if (r.get("published_at") or "") > (release.get("published_at") or "")]
            installed_versions = frozenset(_game_versions_in(f"{asset.get('name')} {_release_text(release)}"))
        latest = next(iter(versions_for(repo, newer, game_version, loader, name, installed_versions)), None)
        if latest:
            file = latest["files"][0]
            same = file["filename"] == name or _norm(latest["version_number"]) == _norm(local.get("version") or "-")
            if not same and apply_install_info(entry, latest):
                from enums import ModStatus
                entry["status"] = ModStatus.UPDATE_AVAILABLE
                logging.info(tr("Найдено обновление для '{0}': {1} -> {2}.", entry["name"], entry["current_version"],
                                entry["new_version"]))
        result[local["path"]] = entry
    cache.save()
    if result:
        logging.info(tr("GitHub: узнано {0} из {1} модов, которых нет в каталогах.", len(result), len(locals_)))
    return result


# --- Для окна: версии и проект в формате Modrinth ---

def get_versions(project: str, game_version: str, loader) -> List[Dict]:
    loader_name = loader[0] if isinstance(loader, (list, tuple)) and loader else loader
    repo = repo_of(project)
    return versions_for(repo, releases(repo, _Cache(_cache_file())), game_version,
                        loader_name if loader_name in LOADERS else None)


def get_project(project: str) -> Dict:
    repo = repo_of(project)
    return {"id": project, "slug": repo.split("/")[-1].lower(), "title": repo.split("/")[-1], "description": "",
            "body": "", "icon_url": f"https://github.com/{repo.split('/')[0]}.png", "source": "github",
            "page_url": page_url(repo), "source_url": page_url(repo), "project_type": "mod", "categories": [],
            "gallery": [], "loaders": []}
