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
from content import CONTENT_TYPES, content_by_key
from enums import ModStatus, UpdateChannel
from game_detection import detect_game_versions

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
    parser = argparse.ArgumentParser(prog="ModUpdater", description="Minecraft Mod Updater — консольный режим")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--update-all", action="store_true", help="обновить все моды профиля")
    action.add_argument("--check", action="store_true", help="только проверить обновления, ничего не меняя")
    action.add_argument("--rollback", action="store_true", help="откатить последнее изменение папки mods")
    action.add_argument("--list-profiles", action="store_true", help="показать профили")
    parser.add_argument("--profile", help="имя профиля (по умолчанию — текущий профиль в GUI)")
    parser.add_argument("--game-dir", help="папка игры/сервера (содержит mods) вместо пути из профиля")
    parser.add_argument("--channel", choices=[c.value for c in UpdateChannel], help="канал обновлений")
    parser.add_argument("--game-version", help="версия Minecraft (переопределяет профиль)")
    parser.add_argument("--loader", choices=["fabric", "forge", "neoforge", "quilt"], help="загрузчик")
    parser.add_argument("--no-deps", action="store_true", help="не устанавливать недостающие обязательные зависимости")
    parser.add_argument("--force", action="store_true", help="не проверять, запущена ли игра")
    parser.add_argument("--content", choices=[c.key for c in CONTENT_TYPES], default="mods",
                        help="что обновлять: моды, шейдеры или ресурспаки")
    parser.add_argument("--no-snapshot", action="store_true", help="не сохранять снимок сборки перед обновлением")
    return parser


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _resolve_versions(profile: dict, args) -> tuple[Optional[str], Optional[str]]:
    game_version, loader = profile.get("game_version"), profile.get("loader")
    if not profile.get("manual_mode"):
        detected = detect_game_versions(profile.get("mods_path", ""))
        if detected.best_guess:
            game_version, loader = detected.best_guess
    return args.game_version or game_version, args.loader or loader


def _collect_updates(mods_data: List[dict], include_deps: bool, pinned=()) -> List[dict]:
    from view_model import pin_key
    statuses = {ModStatus.UPDATE_AVAILABLE, ModStatus.READY_TO_INSTALL}
    if include_deps:
        statuses.add(ModStatus.REQUIRED)
    result = {}
    for mod in mods_data:
        for entry in [mod] + mod.get('dependencies', []):
            pid = entry.get('project_id')
            if (pid and pid not in result and entry.get('download_url') and entry.get('status') in statuses
                    and not entry.get('disabled') and pin_key(entry) not in pinned):
                result[pid] = entry
    return list(result.values())


def run(argv: List[str]) -> int:
    args = build_parser().parse_args(argv)
    cfg = app_config.load_config()

    if args.list_profiles:
        for name, prof in cfg["profiles"].items():
            mark = "*" if name == cfg["current_profile"] else " "
            _print(f"{mark} {name}: {prof.get('mods_path') or '(путь не задан)'} "
                   f"[{prof.get('game_version')}, {prof.get('loader')}, канал {prof.get('channel')}]")
        return EXIT_OK

    profile_name = args.profile or cfg["current_profile"]
    profile = cfg["profiles"].get(profile_name)
    if profile is None:
        if not args.game_dir or args.profile:
            _print(f"Профиль '{profile_name}' не найден. Доступные: {', '.join(cfg['profiles'])}")
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
        _print(f"Папка {content.folder} не найдена: {mods_path}")
        return EXIT_USAGE

    # Импорты здесь, чтобы --list-profiles работал без сетевых зависимостей
    from scanner import scan_mods
    from updater import (UpdateTransaction, create_snapshot, describe_transaction, find_locked_files,
                         get_last_transaction, install_mod, is_game_running, prune_snapshots, rollback_transaction)

    if args.rollback:
        manifest = get_last_transaction(mods_path)
        if not manifest:
            _print("Нет операций, которые можно откатить.")
            return EXIT_OK
        _print(describe_transaction(manifest))
        if not args.force and is_game_running(mods_path):
            _print("Файлы модов заблокированы — закройте игру/сервер или используйте --force.")
            return EXIT_LOCKED
        errors = rollback_transaction(mods_path, manifest)
        for error in errors:
            _print(f"  ! {error}")
        _print("Откат выполнен." if not errors else "Откат выполнен частично.")
        return EXIT_PARTIAL if errors else EXIT_OK

    game_version, loader = _resolve_versions(profile, args)
    if not game_version or not loader:
        _print("Не удалось определить версию игры или загрузчик. Укажите --game-version и --loader.")
        return EXIT_USAGE
    channel = UpdateChannel.from_value(args.channel or profile.get("channel"), app_config.DEFAULT_CHANNEL)

    _print(f"Профиль: {profile_name} | Minecraft {game_version} ({loader}) | канал: {channel.display_name}")
    _print(f"Папка: {mods_path}")
    mods_data = scan_mods(mods_path, game_version, loader, app_config.CACHE_FILE, channel,
                          progress=lambda msg: logging.info(msg), content=content)
    pinned = set(profile.get("pinned") or {})
    updates = _collect_updates(mods_data, include_deps=not args.no_deps, pinned=pinned)
    skipped = [m['name'] for m in mods_data if m.get('download_url') and (m.get('disabled') or m.get('project_id') in pinned)]
    if skipped:
        _print(f"Пропущены (закреплены или выключены): {', '.join(skipped)}")
    not_found = sum(1 for m in mods_data if m['status'] == ModStatus.NOT_FOUND)
    _print(f"{content.title}: {len(mods_data)}, не найдено на Modrinth: {not_found}, к установке/обновлению: {len(updates)}")
    for entry in updates:
        current = entry.get('current_version') or "—"
        _print(f"  • {entry['name']}: {current} -> {entry['new_version']} [{entry['status']}]")

    if args.check or not updates:
        return EXIT_OK

    if not args.force:
        locked = find_locked_files([e.get('filepath') for e in updates])
        if locked or is_game_running(mods_path):
            _print("Файлы модов заблокированы — похоже, игра или сервер запущены. Остановите их или используйте --force.")
            return EXIT_LOCKED

    settings = cfg.get("settings") or {}
    if not args.no_snapshot and settings.get("auto_snapshot", True):
        if create_snapshot(mods_path, "update_all", content):
            prune_snapshots(mods_path, int(settings.get("snapshots_keep", 10)))
            _print("Снимок сборки сохранён (вернуть можно на странице «История» в программе).")

    transaction = UpdateTransaction(mods_path, "update")
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(install_mod, entry, mods_path, transaction): entry for entry in updates}
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                status = future.result()
            except Exception as e:
                logging.exception(f"Ошибка при обновлении {entry['name']}: {e}")
                status = ModStatus.CRITICAL_ERROR
            _print(f"  {'✓' if not status.is_error else '✗'} {entry['name']}: {status.value}")
            if status.is_error:
                failed.append(entry['name'])

    _print(f"Готово: успешно {len(updates) - len(failed)}, ошибок {len(failed)}.")
    if not transaction.is_empty:
        _print("Отменить изменения: --rollback")
    return EXIT_PARTIAL if failed else EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    attach_console()
    app_config.setup_file_logging("mod_updater_cli")
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:  # argparse
        return int(e.code or 0) if isinstance(e.code, int) else EXIT_USAGE
    except Exception as e:
        logging.exception("Непредвиденная ошибка в консольном режиме")
        _print(f"Ошибка: {e}")
        return EXIT_USAGE
