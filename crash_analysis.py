"""
Разбор краш-репорта или лога игры (без Qt): какие моды упоминаются в ошибке.

Счёт подозрения складывается из упоминаний id мода (из fabric.mod.json / mods.toml), slug с Modrinth
и названия: в разделе «Suspected Mods» — сильнее всего, в строках исключений и Mixin — сильнее,
в кадрах стека — слабее.
"""
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from content import enabled_name
from i18n import tr

# Слова, которые встречаются в логах всегда и ничего не говорят о виновнике
IGNORED_TOKENS = {"minecraft", "fabric", "fabricloader", "forge", "neoforge", "quilt", "java", "mixin", "mixins",
                  "api", "lib", "core", "common", "client", "server", "mod", "mods", "the", "and", "for", "net"}
MIN_TOKEN = 4


@dataclass
class Suspect:
    entry: Dict
    score: int
    reasons: List[str] = field(default_factory=list)


@dataclass
class Diagnosis:
    kind: str          # "java" — не та версия Java, "memory" — не хватило памяти
    text: str
    java: int = 0      # Для "java": какая Java нужна (0 — неизвестно)


@dataclass
class CrashReport:
    path: str
    summary: List[str]
    suspects: List[Suspect]
    diagnosis: List[Diagnosis] = field(default_factory=list)


# Номер формата class-файла -> версия Java: 52 — Java 8, 65 — Java 21, 69 — Java 25
_CLASS_VERSION_BASE = 44
_TOO_OLD_JAVA = re.compile(r"class file version (\d+)(?:\.\d+)?\), this version of the Java Runtime only "
                           r"recognizes class file versions up to (\d+)")
_TOO_NEW_JAVA = re.compile(r"Unsupported class file major version (\d+)")
_MEMORY_MARKERS = ("java.lang.OutOfMemoryError", "Description: Out of memory", "GC overhead limit exceeded",
                   "Could not reserve enough space for object heap")


def diagnose(text: str) -> List[Diagnosis]:
    """Вылеты не из-за мода: не та Java или нехватка памяти. Тут искать виноватый мод бесполезно."""
    result = []
    match = _TOO_OLD_JAVA.search(text)
    if match:
        need, have = (int(x) - _CLASS_VERSION_BASE for x in match.groups())
        result.append(Diagnosis("java", tr("Игре или моду нужна Java {0}, а запущена Java {1}.", need, have), need))
    elif "UnsupportedClassVersionError" in text:
        result.append(Diagnosis("java", tr("Игре или моду нужна более новая Java, чем та, на которой её запустили.")))
    match = _TOO_NEW_JAVA.search(text)
    if match and not result:
        have = int(match.group(1)) - _CLASS_VERSION_BASE
        result.append(Diagnosis("java", tr("Загрузчик сборки не работает на Java {0} — нужна более старая Java.", have)))
    if any(marker in text for marker in _MEMORY_MARKERS):
        result.append(Diagnosis("memory", tr("Игре не хватило памяти — выделите ей больше в лаунчере.")))
    return result


def find_crash_files(game_dir: str) -> List[str]:
    """Краш-репорты (новые первыми) и latest.log."""
    files = []
    reports = os.path.join(game_dir, "crash-reports")
    if os.path.isdir(reports):
        files += [os.path.join(reports, f) for f in os.listdir(reports) if f.endswith(".txt")]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    latest = os.path.join(game_dir, "logs", "latest.log")
    if os.path.isfile(latest):
        files.append(latest)
    return files


def mod_ids_from_jar(path: str) -> set:
    """id мода из метаданных jar (Fabric/Quilt/Forge/NeoForge)."""
    ids = set()
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for meta in ("fabric.mod.json", "quilt.mod.json"):
                if meta in names:
                    data = json.loads(z.read(meta).decode("utf-8", errors="ignore"), strict=False)
                    mod_id = data.get("id") or (data.get("quilt_loader") or {}).get("id")
                    if mod_id:
                        ids.add(str(mod_id).lower())
            for toml_path in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if toml_path in names and tomllib:
                    try:
                        data = tomllib.loads(z.read(toml_path).decode("utf-8", errors="ignore"))
                        ids.update(str(m.get("modId")).lower() for m in data.get("mods") or [] if m.get("modId"))
                    except Exception:
                        pass
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
        pass
    return ids


def _tokens(entry: Dict) -> set:
    tokens = set(entry.get("_mod_ids") or ())
    if entry.get("slug"):
        tokens.add(entry["slug"].lower())
    name = re.sub(r"[^a-z0-9]", "", str(entry.get("name") or "").lower())
    if name:
        tokens.add(name)
    if entry.get("filepath"):
        stem = re.split(r"[-_+ ]\d", enabled_name(os.path.basename(entry["filepath"])).lower())[0]
        tokens.add(re.sub(r"[^a-z0-9]", "", stem))
    return {t for t in tokens if len(t) >= MIN_TOKEN and t not in IGNORED_TOKENS}


def _summary(lines: List[str]) -> List[str]:
    """Главное из отчёта: описание и первые строки исключения."""
    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Description:"):
            result.append(stripped)
            for nxt in lines[i + 1:i + 6]:
                if nxt.strip():
                    result.append(nxt.strip())
                    break
        elif re.match(r"^(Caused by: |[\w.$]+(Exception|Error)\b)", stripped) and len(result) < 6:
            result.append(stripped[:300])
    if not result:  # latest.log: последние ошибки
        errors = [line.strip()[:300] for line in lines if "/ERROR]" in line or "Exception" in line]
        result = errors[-5:]
    return list(dict.fromkeys(result))[:6]


def analyze_crash(text: str, entries: List[Dict], path: str = "") -> CrashReport:
    lines = text.splitlines()
    lowered = [line.lower() for line in lines]
    suspected_block = []
    for i, line in enumerate(lowered):
        if "suspected mod" in line:
            suspected_block = lowered[i:i + 15]
            break
    suspects = []
    for entry in entries:
        tokens = _tokens(entry)
        if not tokens:
            continue
        score, reasons = 0, []
        for token in tokens:
            pattern = re.compile(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])")
            if any(pattern.search(line) for line in suspected_block):
                score += 20
                reasons.append(tr("назван в «Suspected Mods»"))
            for line in lowered:
                if not pattern.search(line):
                    continue
                if "mixin" in line or "from mod" in line:
                    score += 6
                    reasons.append(tr("ошибка Mixin этого мода"))
                elif "exception" in line or "caused by" in line or "error" in line:
                    score += 4
                    reasons.append(tr("упоминается в ошибке"))
                elif line.lstrip().startswith("at "):
                    score += 1
                    reasons.append(tr("код мода в стеке вызовов"))
                else:
                    score += 1
        if score:
            suspects.append(Suspect(entry, score, list(dict.fromkeys(reasons))))
    suspects.sort(key=lambda s: -s.score)
    return CrashReport(path, _summary(lines), suspects[:8], diagnose(text))


def analyze_file(path: str, entries: List[Dict]) -> CrashReport:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()[-2_000_000:]  # latest.log бывает огромным — хватает конца
    for entry in entries:
        if "_mod_ids" not in entry and entry.get("filepath") and enabled_name(entry["filepath"]).lower().endswith(".jar"):
            entry["_mod_ids"] = sorted(mod_ids_from_jar(entry["filepath"]))
    return analyze_crash(text, entries, path)
