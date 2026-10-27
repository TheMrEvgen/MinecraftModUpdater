"""
Какая версия загрузчика нужна установленным модам (без Qt).

Новые версии модов часто требуют более свежий загрузчик (Sodium Extra 0.9.6 — Fabric Loader 0.20.1+): обновил моды —
игра не запускается, хотя виноват не мод, а старый загрузчик. Требование читается из самого мода:
  * Fabric — depends.fabricloader в fabric.mod.json («>=0.20.1», «^0.16», список вариантов);
  * Quilt — depends с id quilt_loader в quilt.mod.json;
  * Forge / NeoForge — [[dependencies.<мод>]] с modId forge / neoforge и versionRange «[21.1.77,)» в mods.toml.
"""
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from content import is_disabled

LOADER_IDS = {"fabric": "fabricloader", "quilt": "quilt_loader", "forge": "forge", "neoforge": "neoforge"}
_VERSION = re.compile(r"\d+(?:\.\d+)*(?:[-+][\w.]+)?")


def _newer(a: str, b: str) -> bool:
    from packaging.version import InvalidVersion, parse
    try:
        return parse(a) > parse(b)
    except InvalidVersion:
        return False


def min_from_predicate(predicate) -> Optional[str]:
    """Наименьшая подходящая версия из условия Fabric («>=0.20.1», «^0.16.0», «0.15.x»); у списка — самая мягкая."""
    if isinstance(predicate, list):
        found = [v for v in (min_from_predicate(p) for p in predicate) if v]
        if len(found) != len(predicate):  # Один из вариантов — «любая версия»
            return None
        return min(found, key=lambda v: [int(x) for x in re.findall(r"\d+", v)[:4]])
    text = str(predicate or "").strip()
    if not text or text == "*":
        return None
    lowest = None
    for part in text.split():  # «>=0.15.0 <0.17» — нижняя граница
        if part.startswith("<"):
            continue
        match = _VERSION.search(part.replace(".x", "").replace(".*", ""))
        if match:
            lowest = match.group(0)
    return lowest


def min_from_range(version_range: str) -> Optional[str]:
    """«[21.1.77,)» -> 21.1.77; «[47,)» -> 47; «*» или «» -> None."""
    match = re.match(r"\s*[\[(]\s*([^,\])]*)", version_range or "")
    value = (match.group(1) if match else version_range or "").strip()
    return value if value and _VERSION.fullmatch(value) else None


_CACHE: Dict[tuple, Optional[str]] = {}  # (путь, размер, время, загрузчик) -> требование: моды читаются один раз


def required_version(path: str, loader: str) -> Optional[str]:
    """Версия загрузчика, которую требует мод (None — не требует или не прочитать)."""
    if not LOADER_IDS.get(loader):
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(path), stat.st_size, stat.st_mtime_ns, loader)
    if key not in _CACHE:
        _CACHE[key] = _read_requirement(path, loader)
    return _CACHE[key]


def _read_requirement(path: str, loader: str) -> Optional[str]:
    loader_id = LOADER_IDS[loader]
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if loader == "fabric" and "fabric.mod.json" in names:
                data = json.loads(z.read("fabric.mod.json").decode("utf-8", errors="ignore"), strict=False)
                return min_from_predicate((data.get("depends") or {}).get("fabricloader"))
            if loader == "quilt" and "quilt.mod.json" in names:
                data = json.loads(z.read("quilt.mod.json").decode("utf-8", errors="ignore"), strict=False)
                for dep in (data.get("quilt_loader") or {}).get("depends") or []:
                    if isinstance(dep, dict) and dep.get("id") == loader_id:
                        return min_from_predicate(dep.get("versions"))
                return None
            toml_path = next((p for p in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml") if p in names), None)
            if loader in ("forge", "neoforge") and toml_path and tomllib:
                data = tomllib.loads(z.read(toml_path).decode("utf-8", errors="ignore"))
                for deps in (data.get("dependencies") or {}).values():
                    for dep in deps if isinstance(deps, list) else []:
                        optional = str(dep.get("type", "required")).lower() != "required" or dep.get("mandatory") is False
                        if str(dep.get("modId", "")).lower() == loader_id and not optional:
                            return min_from_range(str(dep.get("versionRange", "")))
    except (OSError, zipfile.BadZipFile, ValueError, AttributeError, TypeError):
        pass
    return None


@dataclass
class Requirement:
    version: str                                   # Самая новая версия загрузчика, которую требуют моды
    mods: List[str] = field(default_factory=list)  # Кто требует больше, чем стоит сейчас


def check(entries: Iterable[Dict], loader: str, current: Optional[str]) -> Optional[Requirement]:
    """Нужен ли модам загрузчик новее текущего. entries — записи модов (filepath, name)."""
    if not current or loader not in LOADER_IDS:
        return None
    needs: Dict[str, str] = {}
    for entry in entries:
        path = entry.get("filepath")
        if not path or entry.get("disabled") or is_disabled(path) or not path.lower().endswith(".jar"):
            continue
        version = required_version(path, loader)
        if version and _newer(version, current):
            needs[entry.get("name") or os.path.basename(path)] = version
    if not needs:
        return None
    top = max(needs.values(), key=lambda v: [int(x) for x in re.findall(r"\d+", v)[:4]])
    return Requirement(top, sorted(needs))
