"""
Текст выпуска на GitHub — раздел версии из CHANGELOG.md.

    python tools/release_notes.py 3.0.0-beta notes.md
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def section(version: str, changelog: str) -> str:
    """Текст под заголовком «## <версия> …» до следующего заголовка второго уровня."""
    match = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    if not match:
        raise SystemExit(f"В CHANGELOG.md нет раздела «## {version}»")
    return match.group(1).strip() + "\n"


def main(argv):
    version, target = argv[0].lstrip("v"), argv[1]
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
        notes = section(version, f.read())
    notes += ("\n---\n**Как установить:** скачайте `ModUpdater.exe` и запустите. Установка в систему не обязательна — "
              "её можно сделать из программы: «Настройки → Установка программы».\n")
    with open(target, "w", encoding="utf-8") as f:
        f.write(notes)


if __name__ == "__main__":
    main(sys.argv[1:])
