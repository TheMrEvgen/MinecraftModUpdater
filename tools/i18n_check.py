"""
Проверка перевода: какие строки из tr(...) и plural(...) ещё не переведены в locales/<язык>.json.

    python tools/i18n_check.py            — отчёт по английскому
    python tools/i18n_check.py --dump     — все строки (JSON) для перевода
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
    files = glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "ui", "*.py"))
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
    keys, catalog = collect_keys(), load_catalog(language)
    return {
        "missing": sorted(k for k in keys if k not in catalog),
        "unused": sorted(k for k in catalog if k not in keys),
        "placeholders": sorted(k for k in keys & catalog.keys() if placeholders(k) != placeholders(catalog[k])),
    }


def main(argv: List[str]) -> int:
    if "--dump" in argv:
        print(json.dumps(sorted(collect_keys()), ensure_ascii=False, indent=1))
        return 0
    report = problems()
    for name, items in report.items():
        print(f"{name}: {len(items)}")
        for item in items[:40]:
            print("   ", repr(item))
    return 1 if report["missing"] or report["placeholders"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
