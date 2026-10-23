"""
Обновление самой программы из выпусков на GitHub (без Qt).

Выпуск содержит ModUpdater.exe и ModUpdater.exe.sha256 (их собирает .github/workflows/release.yml).
Порядок: check() → download() (с проверкой SHA-256) → replace_executable() → перезапуск.
Запущенный .exe Windows не даёт перезаписать, но даёт переименовать: старый файл становится *.old
и удаляется при следующем запуске (cleanup_old).

Бета-версии предлагаются только тем, у кого уже стоит бета; у стабильной версии — только стабильные.
"""
import hashlib
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from packaging.version import InvalidVersion, Version

import config as app_config
from i18n import tr
from mod_updater_logic import SESSION

EXE_ASSET = "ModUpdater.exe"
CHECKSUM_ASSET = "ModUpdater.exe.sha256"
OLD_SUFFIX = ".old"
CHECK_INTERVAL = 12 * 3600  # Автоматическая проверка — не чаще раза в 12 часов
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateCancelled(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page_url: str
    exe_url: str
    checksum_url: str
    size: int
    prerelease: bool


def parse_version(text: str) -> Optional[Version]:
    try:
        return Version((text or "").strip().lstrip("vV"))
    except InvalidVersion:
        return None


def pick_release(releases: List[Dict], current: str, allow_prerelease: bool) -> Optional[Release]:
    """Самый новый подходящий выпуск новее текущей версии (с нужными файлами), иначе None."""
    current_version = parse_version(current)
    best = None
    for release in releases:
        if release.get("draft") or (release.get("prerelease") and not allow_prerelease):
            continue
        version = parse_version(release.get("tag_name") or "")
        if version is None or (current_version is not None and version <= current_version):
            continue
        assets = {a.get("name"): a for a in release.get("assets") or []}
        if EXE_ASSET not in assets or CHECKSUM_ASSET not in assets:
            continue
        if best is None or version > best[0]:
            best = (version, release, assets)
    if best is None:
        return None
    _, release, assets = best
    return Release(
        version=str(release["tag_name"]).lstrip("vV"), tag=release["tag_name"], notes=release.get("body") or "",
        page_url=release.get("html_url") or "", exe_url=assets[EXE_ASSET]["browser_download_url"],
        checksum_url=assets[CHECKSUM_ASSET]["browser_download_url"], size=int(assets[EXE_ASSET].get("size") or 0),
        prerelease=bool(release.get("prerelease")))


def check(current: Optional[str] = None, repository: Optional[str] = None) -> Optional[Release]:
    """Новый выпуск на GitHub или None. Сетевые ошибки пробрасываются."""
    current = current or app_config.APP_VERSION
    repository = app_config.GITHUB_REPOSITORY if repository is None else repository
    if not repository:
        return None
    current_version = parse_version(current)
    allow_prerelease = bool(current_version and current_version.is_prerelease)
    response = SESSION.get(f"https://api.github.com/repos/{repository}/releases", params={"per_page": 30},
                           headers={"Accept": "application/vnd.github+json"}, timeout=15)
    response.raise_for_status()
    return pick_release(response.json(), current, allow_prerelease)


def needs_check(last_check: Optional[float], now: Optional[float] = None) -> bool:
    return not last_check or (now or time.time()) - float(last_check) >= CHECK_INTERVAL


def parse_checksum(text: str) -> str:
    """Первое слово файла .sha256 (формат sha256sum: «хэш  имя»)."""
    value = (text or "").split()[0].strip().lower() if (text or "").split() else ""
    if not SHA256_RE.match(value):
        raise ValueError(tr("Некорректный файл контрольной суммы выпуска."))
    return value


def download(release: Release, folder: str, progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None) -> str:
    """Скачивает новый .exe в folder и проверяет SHA-256. Возвращает путь к файлу."""
    checksum = SESSION.get(release.checksum_url, timeout=15)
    checksum.raise_for_status()
    expected = parse_checksum(checksum.text)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, f"ModUpdater-{release.version}.exe")
    partial = target + ".part"
    digest, done = hashlib.sha256(), 0
    try:
        with SESSION.get(release.exe_url, stream=True, timeout=60) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or release.size or 0)
            with open(partial, "wb") as f:
                for chunk in response.iter_content(256 * 1024):
                    if cancelled and cancelled():
                        raise UpdateCancelled()
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if digest.hexdigest() != expected:
            raise ValueError(tr("Контрольная сумма не совпала — файл повреждён при загрузке. Попробуйте ещё раз."))
        os.replace(partial, target)
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    return target


def current_executable() -> Optional[str]:
    """Путь к запущенному .exe; None, если программа запущена из исходников."""
    return os.path.abspath(sys.executable) if getattr(sys, "frozen", False) else None


def supported() -> bool:
    return sys.platform == "win32" and current_executable() is not None


def replace_executable(new_exe: str, current_exe: str) -> str:
    """Ставит new_exe на место current_exe. Старый файл переименовывается в *.old (его путь возвращается)."""
    old = current_exe + OLD_SUFFIX
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:  # Старый .old ещё занят — берём другое имя
            old = f"{current_exe}.{int(time.time())}{OLD_SUFFIX}"
    os.replace(current_exe, old)
    try:
        shutil.move(new_exe, current_exe)  # Папки могут быть на разных дисках — move скопирует
    except OSError:
        os.replace(old, current_exe)  # Возвращаем как было
        raise
    logging.info(tr("Программа обновлена: {0}", current_exe))
    return old


def cleanup_old(current_exe: Optional[str] = None) -> int:
    """Удаляет *.old, оставшиеся после обновления. Возвращает количество удалённых файлов."""
    exe = current_exe or current_executable()
    if not exe or not os.path.isdir(os.path.dirname(exe)):
        return 0
    folder, name = os.path.split(exe)
    removed = 0
    for entry in os.listdir(folder):
        if entry.startswith(name + ".") and entry.endswith(OLD_SUFFIX):
            try:
                os.remove(os.path.join(folder, entry))
                removed += 1
            except OSError:
                pass  # Ещё занят (старая копия не успела закрыться) — удалится в следующий раз
    return removed


def downloads_folder() -> str:
    return os.path.join(app_config.CONFIG_DIR, "updates")
