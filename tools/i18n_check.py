"""
Проверка перевода: какие строки из tr(...) и plural(...) ещё не переведены в locales/<язык>.json.

    python tools/i18n_check.py                — отчёт по английскому
    python tools/i18n_check.py --lang uk      — отчёт по другому языку
    python tools/i18n_check.py --template uk  — заготовка locales/uk.json: все строки с пустым переводом
                                                (у существующего файла — добавляет недостающие, перевод не трогает)
    python tools/i18n_check.py --dump         — все строки (JSON) для перевода
"""
import ast
import glob
import json
import os
import re
import sys
from typing import Dict, List, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACEHOLDER = re.compile(r"\{(\d+)(?:![rsa])?(?::[^}]*)?\}")


def source_files() -> List[str]:
    files = glob.glob(os.path.join(ROOT, "*.py"))
    for package in ("ui", "window"):
        files += glob.glob(os.path.join(ROOT, package, "*.py"))
    return sorted(f for f in files if os.path.basename(f) not in ("i18n.py",))


def collect_keys() -> Set[str]:
    keys: Set[str] = set()
    for path in source_files():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == "tr" and node.args and isinstance(node.args[0], ast.Constant):
                keys.add(node.args[0].value)
            elif node.func.id == "plural":
                keys.update(a.value for a in node.args[1:] if isinstance(a, ast.Constant) and isinstance(a.value, str))
    return keys


def load_catalog(language: str = "en") -> Dict[str, str]:
    path = os.path.join(ROOT, "locales", f"{language}.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def placeholders(text: str) -> Set[str]:
    return set(PLACEHOLDER.findall(text))


def problems(language: str = "en") -> Dict[str, List[str]]:
    keys = collect_keys()
    catalog = {k: v for k, v in load_catalog(language).items() if not k.startswith("@")}  # «@name» — служебное
    return {
        "missing": sorted(k for k in keys if not catalog.get(k)),
        "unused": sorted(k for k in catalog if k not in keys),
        "placeholders": sorted(k for k in keys & catalog.keys()
                               if catalog[k] and placeholders(k) != placeholders(catalog[k])),
    }


def write_template(language: str) -> str:
    """locales/<код>.json со всеми строками: новые — с пустым переводом, уже переведённые остаются."""
    path = os.path.join(ROOT, "locales", f"{language}.json")
    catalog = load_catalog(language)
    catalog.setdefault("@name", language)
    for key in collect_keys():
        catalog.setdefault(key, "")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(catalog, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return path


def main(argv: List[str]) -> int:
    if "--dump" in argv:
        print(json.dumps(sorted(collect_keys()), ensure_ascii=False, indent=1))
        return 0
    if "--template" in argv:
        language = argv[argv.index("--template") + 1]
        print(f"Заготовка: {write_template(language)} — впишите переводы и укажите название языка в «@name».")
        return 0
    language = argv[argv.index("--lang") + 1] if "--lang" in argv else "en"
    report = problems(language)
    for name, items in report.items():
        print(f"{name}: {len(items)}")
        for item in items[:40]:
            print("   ", repr(item))
    return 1 if report["missing"] or report["placeholders"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
