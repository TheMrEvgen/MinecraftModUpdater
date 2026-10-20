"""
Разметка строк интерфейса для перевода: оборачивает строки с кириллицей в tr(...).

    python tools/i18n_wrap.py file.py ...          — показать, сколько строк будет размечено
    python tools/i18n_wrap.py --write file.py ...  — разметить

f-строки превращаются в tr("шаблон {0}", выражение). Не трогаются: docstring-и и строки-комментарии, вызовы
logging.*, уже размеченные tr(...), функции из SKIP_FUNCS (таблица стилей) и аргументы format_spec.
"""
import ast
import re
import sys
from typing import List, Optional, Tuple

CYRILLIC = re.compile("[А-Яа-яЁё]")
SKIP_FUNCS = {"build_stylesheet"}
SKIP_CALLS = {"logging", "logger", "log"}


def _line_offsets(source: str) -> List[int]:
    offsets, total = [0], 0
    for line in source.splitlines(keepends=True):
        total += len(line)
        offsets.append(total)
    return offsets


class Wrapper:
    def __init__(self, source: str):
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.offsets = _line_offsets(source)
        self.tree = ast.parse(source)
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent  # type: ignore[attr-defined]
        self.skipped_fstrings = 0

    # --- Позиции (col_offset в ast — в байтах UTF-8) ---

    def _char(self, lineno: int, col: int) -> int:
        line = self.lines[lineno - 1]
        return self.offsets[lineno - 1] + len(line.encode("utf-8")[:col].decode("utf-8", errors="ignore"))

    def span(self, node) -> Tuple[int, int]:
        return self._char(node.lineno, node.col_offset), self._char(node.end_lineno, node.end_col_offset)

    # --- Что размечать ---

    @staticmethod
    def _parents(node):
        while hasattr(node, "_parent"):
            node = node._parent
            yield node

    def _excluded(self, node) -> bool:
        parent = getattr(node, "_parent", None)
        if isinstance(parent, ast.Expr):  # docstring или строка-комментарий
            return True
        for ancestor in self._parents(node):
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)) and ancestor.name in SKIP_FUNCS:
                return True
            if isinstance(ancestor, ast.Call):
                func = ancestor.func
                if isinstance(func, ast.Name) and func.id == "tr":
                    return True
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in SKIP_CALLS:
                    return True
            if isinstance(ancestor, ast.FormattedValue) and node is not ancestor and getattr(ancestor, "format_spec", None) \
                    and any(node is n for n in ast.walk(ancestor.format_spec)):
                return True
        return False

    @staticmethod
    def _has_cyrillic(node) -> bool:
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and bool(CYRILLIC.search(node.value))
        if isinstance(node, ast.JoinedStr):
            return any(isinstance(v, ast.Constant) and CYRILLIC.search(v.value) for v in node.values)
        return False

    def _is_target(self, node) -> bool:
        if not isinstance(node, (ast.Constant, ast.JoinedStr)) or not self._has_cyrillic(node):
            return False
        if isinstance(node, ast.Constant) and isinstance(getattr(node, "_parent", None), ast.JoinedStr):
            return False  # Часть f-строки — размечается вместе с ней
        return not self._excluded(node)

    def targets_within(self, node) -> List:
        """Самые внешние размечаемые строки внутри node (не вложенные в другие размечаемые)."""
        found = []
        for child in ast.iter_child_nodes(node):
            if self._is_target(child):
                found.append(child)
            else:
                found.extend(self.targets_within(child))
        return found

    # --- Преобразование ---

    def rewrite(self, node) -> str:
        """Исходный текст node, в котором размечены вложенные строки."""
        start, end = self.span(node)
        text = self.source[start:end]
        for target in sorted(self.targets_within(node), key=lambda n: self.span(n)[0], reverse=True):
            t_start, t_end = self.span(target)
            replacement = self.convert(target)
            if replacement is not None:
                text = text[:t_start - start] + replacement + text[t_end - start:]
        return text

    def convert(self, node) -> Optional[str]:
        if isinstance(node, ast.Constant):
            start, end = self.span(node)
            return f"tr({self.source[start:end]})"
        template, args = "", []
        for value in node.values:
            if isinstance(value, ast.Constant):
                template += value.value.replace("{", "{{").replace("}", "}}")
                continue
            spec = ""
            if value.format_spec is not None:
                if not all(isinstance(v, ast.Constant) for v in value.format_spec.values):
                    self.skipped_fstrings += 1
                    return None  # Динамический формат — оставляем как есть
                spec = ":" + "".join(v.value for v in value.format_spec.values)
            conversion = {-1: "", 115: "!s", 114: "!r", 97: "!a"}[value.conversion]
            template += "{" + str(len(args)) + conversion + spec + "}"
            args.append(self.rewrite(value.value))
        call_args = ", ".join([repr(template)] + args)
        return f"tr({call_args})"

    def run(self) -> Tuple[str, int]:
        targets = sorted(self.targets_within(self.tree), key=lambda n: self.span(n)[0], reverse=True)
        source, count = self.source, 0
        for node in targets:
            replacement = self.convert(node)
            if replacement is None:
                continue
            start, end = self.span(node)
            source = source[:start] + replacement + source[end:]
            count += 1
        return source, count


def add_import(source: str) -> str:
    """from i18n import tr — после последнего импорта верхнего уровня."""
    if re.search(r"^from i18n import .*\btr\b", source, re.MULTILINE):
        return source
    tree = ast.parse(source)
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    lines = source.splitlines(keepends=True)
    line = imports[-1].end_lineno if imports else 0
    if not imports and tree.body and isinstance(tree.body[0], ast.Expr):  # Только docstring
        line = tree.body[0].end_lineno
    lines.insert(line, "from i18n import tr\n")
    return "".join(lines)


def main(argv: List[str]) -> int:
    write = "--write" in argv
    total = 0
    for path in [a for a in argv if not a.startswith("--")]:
        with open(path, encoding="utf-8") as f:
            source = f.read()
        wrapper = Wrapper(source)
        new_source, count = wrapper.run()
        total += count
        print(f"{path}: {count} строк" + (f", пропущено f-строк: {wrapper.skipped_fstrings}" if wrapper.skipped_fstrings else ""))
        if write and count:
            new_source = add_import(new_source)
            compile(new_source, path, "exec")  # Проверка, что код остался корректным
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_source)
    print(f"Всего: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
