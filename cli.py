r"""
Консольный режим для серверов и планировщика задач.

Примеры:
    "Minecraft Mod Updater.exe" --list-profiles
    "Minecraft Mod Updater.exe" --profile Server --check
    "Minecraft Mod Updater.exe" --profile Server --update-all
    "Minecraft Mod Updater.exe" --profile Server --rollback
    "Minecraft Mod Updater.exe" --game-dir D:\server --loader fabric --game-version 1.21.1 --update-all
    "Minecraft Mod Updater.exe" --profile Client --content shaders --update-all

Закреплённые в программе версии и выключенные (*.disabled) моды не обновляются.

Коды возврата: 0 — успех, 1 — часть модов не обновилась, 2 — ошибка параметров/конфигурации,
3 — файлы модов заблокированы (игра или сервер запущены).
"""
import argparse
import concurrent.futures
import logging
import os
import sys
from typing import List, Optional

import config as app_config
from background_check import collect_updates, resolve_versions
from content import CONTENT_TYPES, content_by_key
from enums import ModStatus, UpdateChannel
from i18n import tr

EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, EXIT_LOCKED = 0, 1, 2, 3
CLI_FLAGS = ("--update-all", "--check", "--list-profiles", "--rollback", "--help", "-h")


def wants_cli(argv: List[str]) -> bool:
    return any(arg in CLI_FLAGS or arg.startswith(("--profile", "--game-dir")) for arg in argv[1:])


def attach_console() -> None:
    """В сборке с --windowed у процесса нет консоли: подключаемся к консоли родителя (cmd/планировщик)."""
    if sys.platform != "win32" or (sys.stdout and sys.stdout.isatty()):
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except (OSError, AttributeError):
        pass
    if sys.stdout is None:  # Нет ни консоли, ни перенаправления — пишем в никуда, лог остаётся в файле
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    # Вывод, перенаправленный в файл (например, из планировщика), пишем в UTF-8, а не в ANSI-кодировке
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure") and not stream.isatty():
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ModUpdater", description=tr("Minecraft Mod Updater — консольный режим"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--update-all", action="store_true", help=tr("обновить все моды профиля"))
    action.add_argument("--check", action="store_true", help=tr("только проверить обновления, ничего не меняя"))
    action.add_argument("--rollback", action="store_true", help=tr("откатить последнее изменение папки mods"))
    action.add_argument("--list-profiles", action="store_true", help=tr("показать профили"))
    parser.add_argument("--profile", help=tr("имя профиля (по умолчанию — текущий профиль в GUI)"))
    parser.add_argument("--game-dir", help=tr("папка игры/сервера (содержит mods) вместо пути из профиля"))
    parser.add_argument("--channel", choices=[c.value for c in UpdateChannel], help=tr("канал обновлений"))
    parser.add_argument("--game-version", help=tr("версия Minecraft (переопределяет профиль)"))
    parser.add_argument("--loader", choices=["fabric", "forge", "neoforge", "quilt"], help=tr("загрузчик"))
    parser.add_argument("--no-deps", action="store_true", help=tr("не устанавливать недостающие обязательные зависимости"))
    parser.add_argument("--force", action="store_true", help=tr("не проверять, запущена ли игра"))
    parser.add_argument("--content", choices=[c.key for c in CONTENT_TYPES], default="mods",
                        help=tr("что обновлять: моды, шейдеры или ресурспаки"))
    parser.add_argument("--no-snapshot", action="store_true", help=tr("не сохранять снимок сборки перед обновлением"))
    return parser


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _resolve_versions(profile: dict, args) -> tuple[Optional[str], Optional[str]]:
    return resolve_versions(profile, args.game_version, args.loader)


def run(argv: List[str]) -> int:
    args = build_parser().parse_args(argv)
    cfg = app_config.load_config()

    if args.list_profiles:
        for name, prof in cfg["profiles"].items():
            mark = "*" if name == cfg["current_profile"] else " "
            _print(tr('{0} {1}: {2} [{3}, {4}, канал {5}]', mark, name, prof.get('mods_path') or tr('(путь не задан)'), prof.get('game_version'), prof.get('loader'), prof.get('channel')))
        return EXIT_OK

    profile_name = args.profile or cfg["current_profile"]
    profile = cfg["profiles"].get(profile_name)
    if profile is None:
        if not args.game_dir or args.profile:
            _print(tr("Профиль '{0}' не найден. Доступные: {1}", profile_name, ', '.join(cfg['profiles'])))
            return EXIT_USAGE
        profile = app_config.default_profile()
    if args.game_dir:
        profile = {**profile, "mods_path": os.path.abspath(args.game_dir)}
        if not args.profile:
            profile_name = "(--game-dir)"
            profile["manual_mode"] = False
    content = content_by_key(args.content)
    mods_path = content.path(profile.get("mods_path", ""))
    if not os.path.isdir(mods_path):
        _print(tr('Папка {0} не найдена: {1}', content.folder, mods_path))
        return EXIT_USAGE

    # Импорты здесь, чтобы --list-profiles работал без сетевых зависимостей
    from scanner import scan_mods
    from updater import (UpdateTransaction, create_snapshot, describe_transaction, find_locked_files,
                         get_last_transaction, install_mod, is_game_running, prune_snapshots, rollback_transaction)

    if args.rollback:
        manifest = get_last_transaction(mods_path)
        if not manifest:
            _print(tr("Нет операций, которые можно откатить."))
            return EXIT_OK
        _print(describe_transaction(manifest))
        if not args.force and is_game_running(mods_path):
            _print(tr("Файлы модов заблокированы — закройте игру/сервер или используйте --force."))
            return EXIT_LOCKED
        errors = rollback_transaction(mods_path, manifest)
        for error in errors:
            _print(f"  ! {error}")
        _print(tr("Откат выполнен.") if not errors else tr("Откат выполнен частично."))
        return EXIT_PARTIAL if errors else EXIT_OK

    game_version, loader = _resolve_versions(profile, args)
    if not game_version or not loader:
        _print(tr("Не удалось определить версию игры или загрузчик. Укажите --game-version и --loader."))
        return EXIT_USAGE
    channel = UpdateChannel.from_value(args.channel or profile.get("channel"), app_config.DEFAULT_CHANNEL)

    _print(tr('Профиль: {0} | Minecraft {1} ({2}) | канал: {3}', profile_name, game_version, loader, channel.display_name))
    _print(tr('Папка: {0}', mods_path))
    mods_data = scan_mods(mods_path, game_version, loader, app_config.CACHE_FILE, channel,
                          progress=lambda msg: logging.info(msg), content=content)
    pinned = set(profile.get("pinned") or {})
    updates = collect_updates(mods_data, include_deps=not args.no_deps, pinned=pinned)
    skipped = [m['name'] for m in mods_data if m.get('download_url') and (m.get('disabled') or m.get('project_id') in pinned)]
    if skipped:
        _print(tr('Пропущены (закреплены или выключены): {0}', ', '.join(skipped)))
    not_found = sum(1 for m in mods_data if m['status'] == ModStatus.NOT_FOUND)
    files = {e['filepath'] for m in mods_data for e in [m] + m.get('dependencies', []) if e.get('filepath')}
    _print(tr('{0}: {1}, не найдено на Modrinth: {2}, к установке/обновлению: {3}', content.title, len(files), not_found, len(updates)))
    for entry in updates:
        current = entry.get('current_version') or "—"
        _print(f"  • {entry['name']}: {current} -> {entry['new_version']} [{entry['status']}]")

    if args.check or not updates:
        return EXIT_OK

    if not args.force:
        locked = find_locked_files([e.get('filepath') for e in updates])
        if locked or is_game_running(mods_path):
            _print(tr("Файлы модов заблокированы — похоже, игра или сервер запущены. Остановите их или используйте --force."))
            return EXIT_LOCKED

    settings = cfg.get("settings") or {}
    if not args.no_snapshot and settings.get("auto_snapshot", True):
        if create_snapshot(mods_path, "update_all", content):
            prune_snapshots(mods_path, int(settings.get("snapshots_keep", 10)))
            _print(tr("Снимок сборки сохранён (вернуть можно на странице «История» в программе)."))

    transaction = UpdateTransaction(mods_path, "update")
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(install_mod, entry, mods_path, transaction): entry for entry in updates}
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                status = future.result()
            except Exception as e:
                logging.exception(tr('Ошибка при обновлении {0}: {1}', entry['name'], e))
                status = ModStatus.CRITICAL_ERROR
            _print(f"  {'✓' if not status.is_error else '✗'} {entry['name']}: {status.value}")
            if status.is_error:
                failed.append(entry['name'])

    _print(tr('Готово: успешно {0}, ошибок {1}.', len(updates) - len(failed), len(failed)))
    if not transaction.is_empty:
        _print(tr("Отменить изменения: --rollback"))
    return EXIT_PARTIAL if failed else EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    attach_console()
    app_config.setup_file_logging("mod_updater_cli")
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:  # argparse
        return int(e.code or 0) if isinstance(e.code, int) else EXIT_USAGE
    except Exception as e:
        logging.exception(tr("Непредвиденная ошибка в консольном режиме"))
        _print(tr('Ошибка: {0}', e))
        return EXIT_USAGE
