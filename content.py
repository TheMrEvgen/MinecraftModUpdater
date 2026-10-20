"""
Типы контента сборки: моды, шейдеры и ресурспаки. У каждого своя папка, расширения файлов
и «загрузчики» для запросов к Modrinth (у шейдеров это iris/optifine, у ресурспаков — minecraft).
"""
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple
from i18n import tr

DISABLED_SUFFIX = ".disabled"  # Выключенный файл: mod.jar.disabled (так же делают Prism и MultiMC)


@dataclass(frozen=True)
class ContentType:
    key: str
    folder: str
    title: str
    title_one: str             # «мод», «шейдер», «ресурспак»
    extensions: Tuple[str, ...]
    project_type: str          # project_type в Modrinth
    has_dependencies: bool

    def loaders(self, game_loader: Optional[str]) -> List[str]:
        """Значения loaders для запросов к Modrinth."""
        if self.key == "shaders":
            return ["iris", "optifine", "canvas", "vanilla"]
        if self.key == "resourcepacks":
            return ["minecraft"]
        return [game_loader] if game_loader else []

    def path(self, game_dir: str) -> str:
        return os.path.join(game_dir, self.folder) if game_dir else ""

    def matches(self, filename: str) -> bool:
        name = filename.lower()
        if name.endswith(DISABLED_SUFFIX):
            name = name[:-len(DISABLED_SUFFIX)]
        return name.endswith(self.extensions)


MODS = ContentType("mods", "mods", tr("Моды"), tr("мод"), (".jar",), "mod", True)
SHADERS = ContentType("shaders", "shaderpacks", tr("Шейдеры"), tr("шейдер"), (".zip",), "shader", False)
RESOURCEPACKS = ContentType("resourcepacks", "resourcepacks", tr("Ресурспаки"), tr("ресурспак"), (".zip",), "resourcepack", False)
CONTENT_TYPES = (MODS, SHADERS, RESOURCEPACKS)
BY_KEY = {c.key: c for c in CONTENT_TYPES}


def content_by_key(key: Optional[str]) -> ContentType:
    return BY_KEY.get(key or "", MODS)


def is_disabled(path: str) -> bool:
    return (path or "").lower().endswith(DISABLED_SUFFIX)


def enabled_name(filename: str) -> str:
    """Имя файла без суффикса .disabled."""
    return filename[:-len(DISABLED_SUFFIX)] if is_disabled(filename) else filename
