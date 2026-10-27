"""Игра: запуск, наблюдение, разбор вылета, поиск проблемного мода (вручную и автоматически).

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import logging
import math
import os
import time
from typing import Dict, List, Optional

from PyQt6.QtWidgets import QApplication, QMessageBox

from bisect_session import BisectSession
from content import MODS, enabled_name
from crash_analysis import find_crash_files
from i18n import tr
from mod_updater_logic import format_date
from ui.tools_dialogs import CrashDialog
from ui_components import ask_choice, confirm, show_message
from view_model import iter_all_entries


class GameMixin:
    # ================================================================== Поиск проблемного мода

    def _installed_mods(self) -> List[dict]:
        data = self.mods_data if self.content is MODS else self.sessions.get(self._skey(content=MODS)) or []
        unique = {}
        for entry in iter_all_entries(data):
            if entry.get("filepath"):
                unique.setdefault(os.path.normcase(entry["filepath"]), entry)
        return list(unique.values())

    def _start_bisect(self):
        if self._busy or self._profile().get("bisect"):
            return
        if self.content is not MODS:
            self._set_content(MODS)
        mods = self._installed_mods()
        enabled = [e for e in mods if not e.get("disabled")]
        if len(enabled) < 2:
            self.toasts.show(tr("Для поиска нужно хотя бы два включённых мода."), "warning")
            return
        steps = max(1, math.ceil(math.log2(len(enabled))))
        if not confirm(self, tr("Поиск проблемного мода"),
                       tr('Программа будет по очереди выключать половину модов, а вы — запускать игру и отвечать, осталась ли проблема. Понадобится около {0} запусков игры.\n\nМоды, которые нужны выключенным, выключаются вместе с ними — ложных ошибок из-за зависимостей не будет. В конце всё вернётся как было. Начать?', steps), tr("Начать"), tr("Отмена")):
            return
        name_of = {e["filepath"]: enabled_name(os.path.basename(e["filepath"])) for e in mods}
        by_pid = {e.get("project_id"): name_of[e["filepath"]] for e in mods if e.get("project_id")}
        requires: Dict[str, set] = {}
        for entry in iter_all_entries(self.mods_data):
            if not entry.get("filepath"):
                continue
            for dep in entry.get("dependencies") or []:
                if dep.get("type") == "required" and by_pid.get(dep.get("project_id")):
                    requires.setdefault(name_of[entry["filepath"]], set()).add(by_pid[dep["project_id"]])
        session = BisectSession.start([name_of[e["filepath"]] for e in enabled],
                                      [name_of[e["filepath"]] for e in mods if e.get("disabled")], requires)
        if not self._confirm_game_not_running([e["filepath"] for e in mods]):
            return
        auto = False
        if self._auto_bisect_available():
            plan = self._launch_plan()
            choice = ask_choice(self, QMessageBox.Icon.Question, tr("Как проверять запуск?"),
                                tr('<b>Автоматически</b> — программа сама запускает игру через {0}, ждёт главного меню или вылета, закрывает игру и делает следующий шаг. Подходит, если игра <b>вылетает при запуске</b>.<br><br><b>Вручную</b> — игру запускаете вы и отвечаете кнопками. Подходит для любой проблемы, в том числе в мире.', plan.title),
                                [tr("Автоматически"), tr("Вручную"), tr("Отмена")], default=0, rich_text=True)
            if choice not in (0, 1):
                return
            auto = choice == 0
        self._profile()["bisect"] = session.to_dict()
        self.save_config()
        self._apply_bisect_state(session)
        if auto:
            self._start_auto_step()

    def _bisect_session(self) -> Optional[BisectSession]:
        data = self._profile().get("bisect")
        try:
            return BisectSession.from_dict(data) if data else None
        except (KeyError, TypeError):
            self._profile().pop("bisect", None)
            return None

    def _set_disabled_names(self, disabled_names: set):
        """Приводит папку к состоянию: выключены ровно эти моды (по имени файла без .disabled)."""
        mods = self._installed_mods()
        to_disable = [e for e in mods if not e.get("disabled") and enabled_name(os.path.basename(e["filepath"])) in disabled_names]
        to_enable = [e for e in mods if e.get("disabled") and enabled_name(os.path.basename(e["filepath"])) not in disabled_names]
        errors = self._rename_entries(to_disable, True) + self._rename_entries(to_enable, False)
        self._set_data(self.mods_data)
        self.sessions[self._skey(content=MODS)] = self.mods_data
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Не всё переключилось"),
                         tr("Закройте игру и повторите.\n\n") + "\n".join(errors[:10]))

    def _apply_bisect_state(self, session: BisectSession):
        if session.done:
            self._set_disabled_names(set(session.original_disabled))
            culprit = session.culprit
            entry = next((e for e in self._installed_mods()
                          if enabled_name(os.path.basename(e["filepath"])) == culprit), None)
            self.bisect_banner.show_result(entry.get("name") if entry else culprit)
            if entry:
                self._select_entry(entry)
            return
        self._set_disabled_names(set(session.original_disabled) | set(session.disabled_now))
        self.bisect_banner.show_step(session.step + 1, session.step + session.steps_left(), len(session.disabled_now),
                                     len(session.suspects))

    def _on_bisect_answer(self, problem_persists: bool):
        session = self._bisect_session()
        if not session:
            self.bisect_banner.hide()
            return
        session.answer(problem_persists)
        self._profile()["bisect"] = session.to_dict()
        self.save_config()
        self._apply_bisect_state(session)

    def _finish_bisect(self, keep_culprit_disabled: bool):
        self._auto_bisect = None
        session = self._bisect_session()
        self._profile().pop("bisect", None)
        self.save_config()
        self.bisect_banner.hide()
        if not session:
            return
        disabled = set(session.original_disabled)
        if keep_culprit_disabled and session.culprit:
            disabled.add(session.culprit)
        self._set_disabled_names(disabled)
        self.toasts.show(tr("Поиск завершён: ") + (tr('«{0}» выключен, остальное вернулось как было.', session.culprit)
                                               if keep_culprit_disabled else tr("все моды вернулись как были.")), "success")

    def _cancel_bisect(self):
        if confirm(self, tr("Прервать поиск"), tr("Вернуть все моды в состояние до начала поиска?"), tr("Вернуть"), tr("Продолжить поиск")):
            self._finish_bisect(False)

    def _restore_bisect_banner(self):
        session = self._bisect_session() if self.content is MODS else None
        if not session:
            self.bisect_banner.hide()
        elif session.done:
            self.bisect_banner.show_result(session.culprit)
        else:
            self.bisect_banner.show_step(session.step + 1, session.step + session.steps_left(),
                                         len(session.disabled_now), len(session.suspects))

    # ================================================================== Разбор краша

    def _show_crash_dialog(self, preferred: Optional[str] = None):
        game_dir = self._profile().get("mods_path", "")
        if not game_dir:
            return
        dialog = CrashDialog(self, self.tokens, find_crash_files(game_dir), self._installed_mods(), self._run_task,
                             preferred if isinstance(preferred, str) else None, self._launch_fixes())
        dialog.disable_requested.connect(lambda e: self._toggle_disabled([e]))
        dialog.fix_requested.connect(self._fix_launch_setting)
        dialog.show_requested.connect(self._show_entry_in_list)
        dialog.bisect_requested.connect(self._start_bisect)
        dialog.exec()

    def _show_entry_in_list(self, entry: dict):
        self._switch_page(self.PAGE_MODS_INDEX)
        if self.content is not MODS:
            self._set_content(MODS)
        self._reset_filters()
        self._select_entry(entry)
        self._open_current_details()

    # ================================================================== Игра: запуск, наблюдение, вылеты

    def _game_dir(self) -> str:
        return self._profile().get("mods_path", "")

    def _launch_plan(self):
        import game_runner
        return game_runner.launch_plan(self._profile())

    def _refresh_play_button(self):
        plan = self._launch_plan()
        running = self._game_running
        self.play_btn.setVisible(bool(self._game_dir()))
        if running:
            self.play_btn.setText(tr("Игра запущена"))
            self.play_btn.setEnabled(False)
        else:
            self.play_btn.setText(tr("Играть"))
            self.play_btn.setEnabled(bool(plan) and not self._busy and not self._auto_bisect)
        tip = (tr('Запустить сборку ({0})', plan.title) if plan and plan.opens_instance else
               tr('Открыть {0} (выберите сборку там)', plan.title) if plan else
               tr("Не удалось найти лаунчер — укажите команду запуска в настройках сборки"))
        self.play_btn.setToolTip(tip)

    # ================================================================== Java и память

    def _mod_count(self) -> int:
        folder = MODS.path(self._game_dir())
        try:
            return sum(1 for name in os.listdir(folder) if name.lower().endswith(".jar"))
        except OSError:
            return 0

    def _launch_fixes(self) -> tuple:
        """Что программа умеет исправить в лаунчере этой сборки: память, Java."""
        import java_check
        source = self._profile().get("source")
        return ((("memory",) if source in java_check.FIXABLE else ()) + (("java",) if source in ("prism", "official") else ())
                + (("loader",) if self._launcher_target() else ()))

    def _apply_launch_fixes(self, fixes: List[tuple]) -> bool:
        """fixes — [(вид, значение)]: правит настройки лаунчера, предлагает отменить. False — не получилось."""
        import java_check
        from game_runner import LAUNCHER_TITLES
        profile = self._profile()
        if java_check.launcher_running(profile):
            show_message(self, QMessageBox.Icon.Warning, tr("Закройте лаунчер"),
                         tr("{0} открыт — пока он открыт, новые настройки не подхватятся. Закройте его и повторите.",
                            LAUNCHER_TITLES.get(profile.get("source"), "")))
            return False
        backups, done = [], []
        try:
            for kind, value in fixes:
                if kind == "java":
                    backups.append(java_check.reset_java(profile))
                    done.append(tr("Java выбирает лаунчер"))
                else:
                    backups.append(java_check.set_memory(profile, value))
                    done.append(tr("памяти выделено {0}", java_check.format_gb(value)))
        except (OSError, ValueError) as e:
            for path, backup in reversed(backups):
                java_check.restore(path, backup)
            show_message(self, QMessageBox.Icon.Warning, tr("Не удалось исправить"), str(e))
            return False
        logging.info(tr("Настройки запуска: {0}", ", ".join(done)))

        def undo():
            for path, backup in reversed(backups):
                java_check.restore(path, backup)
            self.toasts.show(tr("Настройки лаунчера возвращены."), "info")
        self.toasts.show(tr("Готово: {0}.", ", ".join(done)), "success", tr("Отменить"), undo, timeout=12000)
        return True

    def _fix_launch_setting(self, kind: str):
        """Кнопка в разборе вылета: не та Java или не хватило памяти."""
        import java_check
        if kind.startswith("loader:"):
            self._update_loader_in_launcher(kind.split(":", 1)[1])
            return
        if kind == "java":
            self._apply_launch_fixes([("java", 0)])
            return
        settings = java_check.read_settings(self._profile())
        total = java_check.total_memory_mb()
        want = java_check.recommended_memory(self._mod_count(), total)
        if settings and settings.memory_mb and settings.memory_mb >= want:  # И так по рекомендации — ещё на 2 ГБ
            want = settings.memory_mb + 2048
            if total and want > total - java_check.SYSTEM_RESERVE_GB * 1024:
                show_message(self, QMessageBox.Icon.Warning, tr("Памяти больше нет"), tr(
                    "Игре уже выделено {0} из {1}. Больше выделить нельзя — уберите тяжёлые моды или шейдеры.",
                    java_check.format_gb(settings.memory_mb), java_check.format_gb(total)))
                return
        self._apply_launch_fixes([("memory", want)])

    def _loader_requirement(self):
        """Нужен ли установленным модам загрузчик новее текущего (читается из самих модов)."""
        import loader_check
        try:
            return loader_check.check(self._installed_mods(), self.loader, self.current_loader_version)
        except Exception as e:
            logging.debug(f"Проверка версии загрузчика: {e}")
            return None

    def _loader_need_text(self, need) -> str:
        from window.common import LOADER_TITLES
        names = ", ".join(need.mods[:4]) + (tr(" и ещё {0}", len(need.mods) - 4) if len(need.mods) > 4 else "")
        return tr("Модам нужен {0} {1} или новее, а у вас {2} ({3}). С ним игра не запустится.",
                  {"fabric": "Fabric Loader", "quilt": "Quilt Loader"}.get(self.loader) or LOADER_TITLES.get(self.loader, self.loader),
                  need.version, self.current_loader_version, names)

    def _check_loader_requirements(self):
        """После обновления модов и чтения папки: если моды требуют загрузчик новее — сразу предложить обновить."""
        need = self._loader_requirement()
        if not need:
            return
        key = (self.current_profile_name, self.current_loader_version, need.version)
        if key in self._loader_warned:
            return
        self._loader_warned.add(key)
        text = self._loader_need_text(need)
        if self._launcher_target():
            self.toasts.show(text, "warning", tr("Обновить загрузчик"),
                             lambda: self._update_loader_in_launcher(need.version), timeout=30000)
        else:
            self.toasts.show(text + " " + tr("Обновите загрузчик в лаунчере."), "warning", timeout=30000)

    def _check_before_launch(self) -> bool:
        """Перед «Играть»: подходит ли Java и хватит ли памяти. False — запуск отменён."""
        import java_check
        profile = self._profile()
        need = self._loader_requirement()
        if need:
            buttons = ([tr("Обновить загрузчик")] if self._launcher_target() else []) + [tr("Играть так"), tr("Отмена")]
            choice = ask_choice(self, QMessageBox.Icon.Warning, tr("Игра не запустится"), self._loader_need_text(need),
                                buttons, default=0)
            if choice is None or buttons[choice] == tr("Отмена"):
                return False
            if buttons[choice] == tr("Обновить загрузчик"):
                self._update_loader_in_launcher(need.version)
                return False
        try:
            issues = java_check.check(profile, self._mod_count())
        except Exception as e:  # Проверка — подсказка: её сбой не должен мешать игре
            logging.debug(f"Проверка Java и памяти: {e}")
            return True
        signature = sorted(f"{i.kind}:{i.value}" for i in issues)
        if not issues or profile.get("launch_check_skipped") == signature:
            return True
        fixable = [i for i in issues if i.fix and i.kind.split("_")[0] in self._launch_fixes()]
        text = "<br><br>".join(f"• {i.text}" for i in issues)
        buttons = ([tr("Исправить и играть")] if fixable else []) + [tr("Играть так"), tr("Отмена")]
        choice = ask_choice(self, QMessageBox.Icon.Warning, tr("Игра может не запуститься"), text, buttons,
                            default=0, rich_text=True)
        if choice is None or buttons[choice] == tr("Отмена"):
            return False
        if buttons[choice] == tr("Играть так"):
            profile["launch_check_skipped"] = signature  # Не спрашивать снова, пока ничего не поменялось
            self.save_config()
            return True
        return self._apply_launch_fixes([(i.kind.split("_")[0], i.value) for i in fixable])

    def _play(self):
        import game_runner
        plan = self._launch_plan()
        if not plan:
            self.toasts.show(tr("Не удалось найти лаунчер для этой сборки."), "warning", tr("Настроить"), self._edit_current_profile)
            return
        if not self._check_before_launch():
            return
        profile = self._profile()
        installation = profile.get("launcher_id") if profile.get("source") == "official" else None
        if installation:  # Лаунчер откроется сразу с этой установкой: она станет «последней запущенной»
            import official_launcher
            official_launcher.mark_last_used(official_launcher.minecraft_dir(), installation)
        try:
            game_runner.launch(plan)
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось запустить"), str(e))
            return
        self._launched_at = time.time()
        if plan.opens_instance:
            text = tr('Запускаем игру через {0}…', plan.title)
        elif installation:
            text = tr('Открыт {0} с установкой «{1}» — нажмите «Играть».', plan.title, self.current_profile_name)
        else:
            text = tr('Открыт {0} — запустите сборку «{1}» там.', plan.title, self.current_profile_name)
        self.toasts.show(text, "info")

    def _poll_game(self):
        """Раз в несколько секунд: запущена ли игра этой сборки (файлы модов заняты) и не вылетела ли она."""
        import game_runner
        from updater import is_game_running
        game_dir = self._game_dir()
        mods = MODS.path(game_dir)
        if not game_dir or not os.path.isdir(mods) or self._busy == "update":
            return
        running = is_game_running(mods)
        if self._auto_bisect:
            self._auto_bisect_poll(running)
            return
        if running and not self._game_running:
            self._game_started_at = self._launched_at or time.time()
            self._set_game_running(True)
        elif not running and self._game_running:
            self._set_game_running(False)
            self._after_game_exit(self._game_started_at)
        elif not running and self._launched_at and time.time() - self._launched_at < 300:
            # Игра могла упасть ещё до того, как заняла файлы модов (ошибка загрузчика)
            state = game_runner.run_state(game_dir, self._launched_at)
            if state.crashed:
                launched, self._launched_at = self._launched_at, None
                self._after_game_exit(launched, state)

    def _set_game_running(self, running: bool):
        self._game_running = running
        self.game_banner.setVisible(running)
        self._refresh_play_button()
        self._refresh_actions()

    def _after_game_exit(self, started_at: Optional[float], state=None):
        import game_runner
        self._launched_at = None
        game_dir = self._game_dir()
        state = state or game_runner.run_state(game_dir, started_at or time.time() - 3600)
        self._record_launch(started_at, state)
        if not state.crashed:
            return
        logging.info(tr('Игра завершилась с ошибкой: {0}', state.reason))
        QApplication.alert(self)
        if self.settings.get("auto_crash_analysis", True):
            self.toasts.show(tr("Игра вылетела — открываем разбор."), "warning")
            self._show_crash_dialog(preferred=state.crash_file)
        else:
            self.toasts.show(tr("Игра вылетела."), "warning", tr("Разобрать"), lambda: self._show_crash_dialog(state.crash_file),
                             timeout=12000)
        # После окна разбора: если вылеты начались с конкретного изменения модов — предлагаем его откатить
        self._suggest_rollback_after_crash()

    def _record_launch(self, started_at: Optional[float], state):
        """Запуск — в историю сборки: дошла ли игра до меню, вылетела ли, сколько грузилась."""
        import game_runner
        import launch_history
        if not started_at or self._bisect_session():  # Запуски при поиске проблемного мода — не в счёт
            return
        run = {"start": started_at, "menu": bool(state.reached_menu), "crashed": bool(state.crashed),
               "load": game_runner.load_seconds(self._game_dir(), started_at)}
        profile = self._profile()
        profile["launches"] = launch_history.record(profile.get("launches"), run)
        self.save_config()
        if run["load"]:
            logging.info(tr("Игра загрузилась за {0} с", run["load"]))

    def _suggest_rollback_after_crash(self) -> bool:
        """До операции с модами игра запускалась, после — вылетает: предлагаем откатить именно её."""
        import launch_history
        from updater import list_transactions
        mods_dir = MODS.path(self._game_dir())
        manifest = launch_history.suspect_operation(self._profile().get("launches"), list_transactions(mods_dir))
        if not manifest:
            return False
        when = format_date(manifest.get("created")) + " " + (manifest.get("created") or "")[11:16]
        self.toasts.show(tr("До изменения модов {0} игра запускалась, а после — вылетает ({1}). Откатить это изменение?",
                            when, launch_history.changed_mods(manifest)), "warning", tr("Откатить"),
                         lambda: self._rollback_from_history(manifest, mods_dir), timeout=30000)
        return True

    def _stop_game(self):
        import game_runner
        if confirm(self, tr("Закрыть игру"), tr("Закрыть Minecraft этой сборки? Несохранённый прогресс в мире может потеряться."),
                   tr("Закрыть"), tr("Отмена")):
            self._run_task(game_runner.stop_game, (self._game_dir(),),
                           lambda n: self.toasts.show(tr("Игра закрыта.") if n else tr("Процесс игры не найден."), "info"),
                           lambda e: self.toasts.show(tr('Не удалось закрыть игру: {0}', e), "error"), None)

    # ================================================================== Автоматический поиск проблемного мода

    def _auto_bisect_available(self) -> bool:
        plan = self._launch_plan()
        return bool(plan and plan.opens_instance)

    def _start_auto_step(self):
        """Шаг автопоиска: состояние модов уже применено — запускаем игру и ждём меню или вылета."""
        import game_runner
        session = self._bisect_session()
        if not session or session.done:
            self._auto_bisect = None
            return
        plan = self._launch_plan()
        self._auto_bisect = {"phase": "starting", "since": time.time(), "seen_running": False}
        try:
            game_runner.launch(plan)
        except OSError as e:
            self._auto_bisect = None
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось запустить игру"), str(e))
            self._restore_bisect_banner()
            return
        self._show_auto_status(tr("запускаем игру и ждём главного меню…"))

    def _show_auto_status(self, text: str):
        session = self._bisect_session()
        if session:
            self.bisect_banner.show_auto(session.step + 1, session.step + session.steps_left(),
                                         len(session.disabled_now), len(session.suspects), text)

    def _auto_bisect_poll(self, running: bool):
        import game_runner
        auto = self._auto_bisect
        if not auto or auto.get("phase") != "starting":
            return
        auto["seen_running"] = auto["seen_running"] or running
        state = game_runner.run_state(self._game_dir(), auto["since"])
        timeout = int(self.settings.get("auto_bisect_timeout", 5)) * 60
        if state.crashed:
            self._auto_answer(True, tr('вылет ({0})', state.reason))
        elif state.reached_menu:
            self._auto_answer(False, tr("игра дошла до меню"))
        elif auto["seen_running"] and not running:
            self._auto_answer(True, tr("игра закрылась, не дойдя до меню"))
        elif time.time() - auto["since"] > timeout:
            self._auto_bisect = None
            self.toasts.show(tr("Игра не загрузилась за отведённое время — ответьте на этот шаг вручную."), "warning")
            self._restore_bisect_banner()
        elif auto["seen_running"]:
            self._show_auto_status(tr("игра загружается…"))

    def _auto_answer(self, problem_persists: bool, reason: str):
        import game_runner
        self._auto_bisect["phase"] = "stopping"
        logging.info(tr('Автопоиск: {0} → проблема {1}', reason, tr('осталась') if problem_persists else tr('пропала')))
        self._show_auto_status(tr('{0}; закрываем игру…', reason))
        game_dir = self._game_dir()

        def wait_closed():
            from updater import is_game_running
            game_runner.stop_game(game_dir)
            deadline = time.time() + 30
            while is_game_running(MODS.path(game_dir)) and time.time() < deadline:
                time.sleep(1)

        def next_step(_=None):
            if not self._auto_bisect:  # Остановлено пользователем
                return
            self._on_bisect_answer(problem_persists)
            session = self._bisect_session()
            if session and not session.done:
                self._start_auto_step()
            else:
                self._auto_bisect = None

        self._run_task(wait_closed, (), next_step, lambda e: next_step(), None)

    def _stop_auto_bisect(self):
        self._auto_bisect = None
        self.toasts.show(tr("Автоматический поиск остановлен — продолжайте вручную или прервите поиск."), "info")
        self._restore_bisect_banner()
