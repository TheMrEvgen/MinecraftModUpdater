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
    per_world: bool = False    # Папка своя у каждого мира: saves/<мир>/<folder> (датапаки)

    def loaders(self, game_loader: Optional[str]) -> List[str]:
        """Значения loaders для запросов к Modrinth."""
        if self.key == "shaders":
            return ["iris", "optifine", "canvas", "vanilla"]
        if self.key == "resourcepacks":
            return ["minecraft"]
        if self.key == "datapacks":
            return ["datapack"]
        if self.key == "modpacks":
            return []  # У сборки свой загрузчик — фильтровать по загрузчику текущей сборки незачем
        return [game_loader] if game_loader else []

    def path(self, game_dir: str, world: Optional[str] = None) -> str:
        """Папка этого типа контента. Для датапаков нужен мир; без него — пустая строка."""
        if not game_dir:
            return ""
        if self.per_world:
            return os.path.join(game_dir, "saves", world, self.folder) if world else ""
        return os.path.join(game_dir, self.folder)

    def matches(self, filename: str) -> bool:
        name = filename.lower()
        if name.endswith(DISABLED_SUFFIX):
            name = name[:-len(DISABLED_SUFFIX)]
        return name.endswith(self.extensions)


MODS = ContentType("mods", "mods", tr("Моды"), tr("мод"), (".jar",), "mod", True)
SHADERS = ContentType("shaders", "shaderpacks", tr("Шейдеры"), tr("шейдер"), (".zip",), "shader", False)
RESOURCEPACKS = ContentType("resourcepacks", "resourcepacks", tr("Ресурспаки"), tr("ресурспак"), (".zip",), "resourcepack", False)
DATAPACKS = ContentType("datapacks", "datapacks", tr("Датапаки"), tr("датапак"), (".zip",), "datapack", False, per_world=True)
CONTENT_TYPES = (MODS, SHADERS, RESOURCEPACKS, DATAPACKS)
# Готовые сборки — только в каталоге: они ставятся новой сборкой, а не в папку текущей
MODPACKS = ContentType("modpacks", "", tr("Сборки"), tr("сборка"), (".mrpack", ".zip"), "modpack", False)
CATALOG_TYPES = CONTENT_TYPES + (MODPACKS,)
BY_KEY = {c.key: c for c in CONTENT_TYPES}


def content_by_key(key: Optional[str]) -> ContentType:
    return BY_KEY.get(key or "", MODS)


def is_disabled(path: str) -> bool:
    return (path or "").lower().endswith(DISABLED_SUFFIX)


def enabled_name(filename: str) -> str:
    """Имя файла без суффикса .disabled."""
    return filename[:-len(DISABLED_SUFFIX)] if is_disabled(filename) else filename


def list_worlds(game_dir: str) -> List[str]:
    """Миры в saves (папки с level.dat), последние сыгранные — первыми. Сами миры программа не трогает."""
    saves = os.path.join(game_dir or "", "saves")
    if not game_dir or not os.path.isdir(saves):
        return []
    worlds = []
    for name in os.listdir(saves):
        level = os.path.join(saves, name, "level.dat")
        if os.path.isfile(level):
            worlds.append((os.path.getmtime(level), name))
    return [name for _, name in sorted(worlds, reverse=True)]


def pick_world(game_dir: str, wanted: Optional[str]) -> Optional[str]:
    """Выбранный мир, если он ещё есть, иначе последний сыгранный."""
    worlds = list_worlds(game_dir)
    return wanted if wanted in worlds else (worlds[0] if worlds else None)
