"""Окна готовой сборки из каталога: установка (что внутри, куда ставить) и обновление до новой версии."""
import logging
import os
from typing import Dict, Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLineEdit, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QTextBrowser, QWidget,
)

import manual_files
import modpack_install
from i18n import tr
from ui.dialogs import FramelessDialog
from ui.widgets import button, label
from ui_components import confirm
from workers import SafeThread

LOADER_TITLES = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}


class _Worker(SafeThread):
    """
    Установка: prepare — скачать файл сборки и прочитать её, install — поставить в выбранное место и запомнить состав.
    Обновление: prepare_update — скачать новую версию и сравнить с поставленной, update — обновить.
    """
    progress = pyqtSignal(int, int, str)
    prepared = pyqtSignal(object)
    installed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, job: str, project_id: str = "", folder: str = "", plan=None, target=None, title: str = "",
                 game_dir: str = "", update=None, download=None):
        super().__init__()
        self.job, self.project_id, self.folder, self.plan, self.target = job, project_id, folder, plan, target
        self.title, self.game_dir, self.update = title, game_dir, update
        self.download = download  # downloads.DownloadJob — ход загрузки на странице «Загрузки»
        self._cancelled = False

    def stop(self):
        self._cancelled = True

    def run(self):
        try:
            if self.job == "prepare":
                info = modpack_install.latest_pack_file(self.project_id)
                plan = modpack_install.load_plan(modpack_install.download_pack(info, self.folder))
                plan.source_file = info  # Какой именно выпуск поставлен — для проверки обновлений и докачки
                manual_files.resolve_manual(plan)  # «Только с сайта», лежащие на Modrinth, — скачаем оттуда
                self.prepared.emit(plan)
            elif self.job == "install":
                errors = modpack_install.install(self.plan, self.target, self.progress.emit, lambda: self._cancelled,
                                                 job=self.download)
                try:
                    if not self.target.curseforge_zip:  # Архив для CurseForge App: сборку поставит приложение
                        modpack_install.save_state(self.plan, self.project_id, self.target.game_dir, self.title)
                except OSError as e:  # Без записи сборка работает, только не обновится из каталога
                    logging.warning(tr("Сборка: не удалось запомнить состав: {0}", e))
                self.installed.emit(errors)
            elif self.job == "retry":  # Докачать файлы, которые не скачались при установке (update — их пути)
                self.installed.emit(modpack_install.download_missing(self.plan, self.game_dir, self.update,
                                                                     self.progress.emit, lambda: self._cancelled,
                                                                     job=self.download))
            elif self.job == "prepare_update":
                info = modpack_install.latest_pack_file(self.project_id)
                plan = modpack_install.load_plan(modpack_install.download_pack(info, self.folder))
                plan.source_file = info
                manual_files.resolve_manual(plan)
                state = modpack_install.load_state(self.game_dir) or {}
                changelog = modpack_install.pack_changelog(self.project_id, state.get("version") or "", plan.version,
                                                           plan.game_version or "")
                self.prepared.emit((plan, modpack_install.plan_update(state, plan, self.game_dir), changelog))
            elif self.job == "prepare_repair":  # Сверить сборку с её выпуском
                state = modpack_install.load_state(self.game_dir) or {}
                plan = modpack_install.installed_plan(state, self.folder)
                modpack_install.remember_source(self.game_dir, plan)
                manual_files.resolve_manual(plan)
                self.prepared.emit((plan, modpack_install.plan_repair(plan, self.game_dir, self.progress.emit,
                                                                      lambda: self._cancelled)))
            elif self.job == "repair":
                self.installed.emit(modpack_install.apply_repair(self.update, self.plan, self.game_dir,
                                                                 self.progress.emit, lambda: self._cancelled,
                                                                 job=self.download))
            else:
                self.installed.emit(modpack_install.apply_update(self.update, self.plan, self.game_dir,
                                                                 self.progress.emit, lambda: self._cancelled,
                                                                 job=self.download))
        except modpack_install.InstallCancelled:
            self.failed.emit("")
        except Exception as e:
            self.failed.emit(str(e))


def manual_summary(plan) -> str:
    """Что будет с файлами «только с сайта»: сколько возьмём с Modrinth сами и сколько придётся скачать вручную."""
    parts = []
    if plan.from_modrinth:
        parts.append(tr("Файлов, которые автор разрешает скачивать только с сайта CurseForge, но точно такие же лежат на "
                        "Modrinth: {0} — скачаем их оттуда сами.", plan.from_modrinth))
    if plan.manual:
        similar = sum(1 for f in plan.manual if f.path in plan.substitutes)
        text = tr("Скачать вручную с сайта CurseForge придётся {0}: {1}.", len(plan.manual),
                  ", ".join(f.name for f in plan.manual[:4]) + ("…" if len(plan.manual) > 4 else ""))
        if similar:
            text += " " + tr("Для {0} из них есть похожие на Modrinth — можно будет скачать их.", similar)
        parts.append(text + " " + tr("После установки поможем: откроем страницы и сами заберём файлы из «Загрузок»."))
    return "".join("\n" + p for p in parts)


class ModpackInstallDialog(FramelessDialog):
    """После accept(): plan и target — что и куда ставить. Сама установка идёт в фоне (окно её запускает)."""

    RADIO_TEXTS = {
        "prism": tr("Prism Launcher — «Играть» работает сразу"),
        "multimc": "MultiMC",
        "official": tr("Minecraft Launcher — своя установка"),
        "modrinth": tr("Modrinth App (закройте его на время установки)"),
        "curseforge": tr("CurseForge App — архив для импорта"),
    }

    def __init__(self, parent, hit: Dict, launchers: Dict[str, str], default_folder: str, downloads: str):
        super().__init__(parent, tr("Установка сборки"), 580)
        self.hit = hit
        self.launchers = launchers  # Лаунчеры на компьютере: modpack_install.available_launchers()
        self.plan = None
        self.target = None
        self.errors = []
        self.worker: Optional[_Worker] = None
        self.body.addWidget(label(hit.get("title") or "", "SectionTitle"))
        self.info = label(tr("Скачиваем сборку и смотрим, что внутри…"), "Muted", wrap=True)
        self.body.addWidget(self.info)

        self.body.addWidget(label(tr("Название"), "Faint"))
        self.name_edit = QLineEdit(hit.get("title") or "")
        self.body.addWidget(self.name_edit)
        self.body.addWidget(label(tr("Куда поставить"), "Faint"))
        self.where = QButtonGroup(self)
        self.radios: Dict[str, QRadioButton] = {}
        for kind, text in self.RADIO_TEXTS.items():
            radio = QRadioButton(text)
            radio.setVisible(kind in launchers and kind != "official")  # Minecraft Launcher — когда прочитаем сборку
            self.where.addButton(radio)
            self.body.addWidget(radio)
            self.radios[kind] = radio
        self.folder_radio = QRadioButton(tr("Папка на диске (для своего лаунчера)"))
        self.where.addButton(self.folder_radio)
        self.body.addWidget(self.folder_radio)
        self.prism_radio, self.official_radio = self.radios["prism"], self.radios["official"]
        first = next((r for k, r in self.radios.items() if k in launchers and k != "official"), self.folder_radio)
        first.setChecked(True)
        row = QHBoxLayout()
        self.folder_edit = QLineEdit(default_folder)
        browse = button(tr("Обзор…"), None, "folder", parent.tokens, "text")
        browse.clicked.connect(self._browse)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        self.body.addLayout(row)
        # Своя папка для сборки — только в общей папке по умолчанию; выбранную вручную папку считаем папкой игры
        self.default_folder = default_folder
        self.own_folder = QCheckBox(tr("Создать внутри папку с названием сборки"))
        self.own_folder.setChecked(True)
        self.body.addWidget(self.own_folder)
        self.loader_here = QCheckBox(tr("Поставить загрузчик в папку versions"))
        self.loader_here.setToolTip(tr("Для Legacy Launcher, TLauncher и других лаунчеров, которые берут версии игры "
                                       "из папки игры"))
        self.body.addWidget(self.loader_here)
        self.folder_hint = label("", "Faint", wrap=True)
        self.body.addWidget(self.folder_hint)
        self.curseforge_hint = label(tr("Архив появится в папке «Загрузки». В CurseForge App: «Создать профиль» → "
                                        "«Импорт» → выберите этот архив."), "Faint", wrap=True)
        self.body.addWidget(self.curseforge_hint)
        self.loader_text = "Fabric"
        self.own_folder.toggled.connect(lambda *_: self._refresh_where())
        self.loader_here.toggled.connect(lambda *_: self._refresh_where())
        self.folder_edit.textEdited.connect(self._folder_chosen)
        self.where.buttonToggled.connect(lambda *_: self._refresh_where())

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.body.addWidget(self.progress)
        self.progress_text = label("", "Faint", wrap=True)
        self.body.addWidget(self.progress_text)
        self.body.addStretch()

        self.cancel_btn = self.add_button(tr("Отмена"), False, self._cancel)
        self.install_btn = self.add_button(tr("Установить"), True, self._install)
        self.install_btn.setEnabled(False)
        self.finish_layout()
        self._refresh_where()
        self._start(_Worker("prepare", hit.get("project_id"), downloads))

    def _start(self, worker: _Worker):
        self.worker = worker
        worker.progress.connect(self._on_progress)
        worker.prepared.connect(self._on_prepared)
        worker.installed.connect(self._on_installed)
        worker.failed.connect(self._on_failed)
        worker.start()

    def _kind(self) -> str:
        """Куда ставим: ключ лаунчера или "folder"."""
        return next((k for k, r in self.radios.items() if r.isChecked() and k in self.launchers), "folder")

    def _refresh_where(self):
        folder = self.folder_radio.isChecked()
        self.folder_edit.setEnabled(folder)
        for widget in (self.own_folder, self.loader_here, self.folder_hint):
            widget.setVisible(folder)
        self.curseforge_hint.setVisible(self.radios["curseforge"].isChecked())
        where = (tr("Внутри появится папка сборки — она и будет папкой игры.") if self.own_folder.isChecked()
                 else tr("Моды, настройки и карты лягут прямо в эту папку (в mods, config, saves…)."))
        if self.loader_here.isChecked():
            where += " " + tr("Загрузчик {0} появится в списке версий лаунчера (Legacy Launcher, TLauncher и похожих).",
                              self.loader_text)
        else:
            where += " " + tr("Загрузчик ({0}) поставьте в лаунчере сами, указав эту папку как папку игры.",
                              self.loader_text)
        self.folder_hint.setText(where)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, tr("Куда поставить сборку"), self.folder_edit.text())
        if path:
            self.folder_edit.setText(path)
            self._folder_chosen(path)

    def _folder_chosen(self, path: str):
        """Выбрали конкретную папку — ставим прямо в неё, а не в подпапку с названием сборки."""
        def norm(p):
            return os.path.normcase(os.path.normpath(p.strip() or "."))
        self.own_folder.setChecked(norm(path) == norm(self.default_folder))
        # Папка лаунчера с versions (как .minecraft) — загрузчик туда же
        self.loader_here.setChecked(bool(path.strip()) and modpack_install.looks_like_launcher_dir(path.strip()))

    def _on_prepared(self, plan):
        from view_model import format_size
        self.plan = plan
        self.progress.hide()
        loader = LOADER_TITLES.get(plan.loader, plan.loader or "?")
        text = tr("Minecraft {0} · {1} {2} · файлов: {3}", plan.game_version or "?", loader, plan.loader_version or "",
                  len(plan.files))
        if plan.version:
            text = tr("Версия сборки {0}. ", plan.version) + text
        size = plan.size()
        if size:
            text += " · " + tr("займёт около {0}", format_size(size))
        text += manual_summary(plan)
        self.info.setText(text)
        self.loader_text = f"{loader} {plan.loader_version or ''}".strip()
        self.loader_here.setEnabled(bool(plan.loader and plan.loader_version))
        if not self.loader_here.isEnabled():
            self.loader_here.setChecked(False)
        self._refresh_where()
        import official_launcher
        official = bool("official" in self.launchers and official_launcher.can_install(plan.loader) and plan.loader_version)
        self.official_radio.setVisible(official)
        if plan.loader in official_launcher.INSTALLER_LOADERS:
            self.official_radio.setText(self.RADIO_TEXTS["official"] + " · " + tr("нужна Java"))
            self.official_radio.setToolTip(tr("{0} ставится своим установщиком — программа запустит его сама на Java, "
                                              "найденной на компьютере.", loader))
        if official and not any(k in self.launchers for k in ("prism", "multimc")):
            self.official_radio.setChecked(True)
        self.install_btn.setEnabled(bool(plan.game_version))

    def _install(self):
        from view_model import format_size
        name = self.name_edit.text().strip() or self.plan.name
        kind = self._kind()
        if kind != "folder":
            self.target = modpack_install.make_target(kind, self.launchers[kind], name)
            if kind == "modrinth":
                import modrinth_app
                if modrinth_app.running():
                    self.progress_text.setText(tr("Закройте Modrinth App — пока оно открыто, новую сборку в него не "
                                                  "добавить."))
                    return
        else:
            folder = self.folder_edit.text().strip()
            if not folder:
                self.progress_text.setText(tr("Укажите папку для сборки."))
                return
            own = self.own_folder.isChecked()
            if not own and modpack_install.folder_has_mods(folder) and not confirm(
                    self, tr("Папка уже занята"),
                    tr("В папке «{0}» уже есть моды. Поставить сборку прямо сюда? Файлы с такими же именами "
                       "заменятся, остальные останутся.", folder), tr("Поставить сюда"), tr("Отмена")):
                return
            self.target = modpack_install.folder_target(folder, name, own_folder=own,
                                                        loader_here=self.loader_here.isChecked())
        need, free = self.plan.size(), modpack_install.free_space(self.target.curseforge_zip or self.target.game_dir)
        if need and free is not None and free < need * 1.1 and not confirm(
                self, tr("Мало места на диске"),
                tr("Сборке нужно около {0}, а на диске свободно {1}. Установка может оборваться на середине.\n\n"
                   "Всё равно ставить?", format_size(need), format_size(free)), tr("Ставить"), tr("Отмена")):
            self.target = None
            return
        self.accept()  # Качается в фоне: ход — в индикаторе и на странице «Загрузки»

    def _on_progress(self, done: int, total: int, text: str):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.progress_text.setText(tr("{0} из {1}: {2}", done, total, text))

    def _on_installed(self, errors):
        self.errors = errors
        self.accept()

    def _on_failed(self, error: str):
        if not error:
            super().reject()
            return
        self.progress.hide()
        self.progress_text.setText(tr("Не удалось: {0}", error))
        self.install_btn.setEnabled(self.plan is not None)

    def _cancel(self):
        if self.worker and self.worker.isRunning():
            self.progress_text.setText(tr("Отменяем…"))
            self.worker.stop()
            if self.worker.job == "prepare":
                self.worker.wait(3000)
                super().reject()
        else:
            super().reject()

    def reject(self):
        self._cancel()


class ModpackUpdateDialog(FramelessDialog):
    """Обновление поставленной сборки. После accept(): plan (новая версия) и update (что поменять) — само
    обновление идёт в фоне (окно его запускает)."""

    def __init__(self, parent, state: Dict, game_dir: str, downloads: str):
        super().__init__(parent, tr("Обновление сборки"), 640)
        self.state = state
        self.game_dir = game_dir
        self.plan = None
        self.update = None
        self.backup = ""
        self.worker: Optional[_Worker] = None
        self.body.addWidget(label(state.get("title") or "", "SectionTitle"))
        self.info = label(tr("Скачиваем новую версию сборки и сравниваем с вашей…"), "Muted", wrap=True)
        self.body.addWidget(self.info)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.body.addWidget(self.progress)
        self.progress_text = label("", "Faint", wrap=True)
        self.body.addWidget(self.progress_text)
        self.changes_title = label(tr("Что нового"), "SectionTitle")
        self.changes = QTextBrowser()
        self.changes.setOpenExternalLinks(True)
        self.changes.setMinimumHeight(220)
        for widget in (self.changes_title, self.changes):
            widget.hide()
            self.body.addWidget(widget)
        self.body.addStretch()
        self.cancel_btn = self.add_button(tr("Отмена"), False, self._cancel)
        self.update_btn = self.add_button(tr("Обновить"), True, self._apply)
        self.update_btn.setEnabled(False)
        self.finish_layout()
        self._start(_Worker("prepare_update", state.get("project_id") or "", downloads, game_dir=game_dir))

    def _start(self, worker: _Worker):
        self.worker = worker
        worker.progress.connect(self._on_progress)
        worker.prepared.connect(self._on_prepared)
        worker.installed.connect(self._on_updated)
        worker.failed.connect(self._on_failed)
        worker.start()

    def _on_prepared(self, result):
        from ui.dialogs import demote_headings, prepare_markdown
        self.plan, self.update, changelog = result
        if changelog:  # Изменения всех выпусков между вашей версией и новой — от новой к старой
            parts = [f"**{version}**\n\n{prepare_markdown(text).strip() or tr('_Автор не описал изменения._')}"
                     for version, text in changelog]
            self.changes.setMarkdown(demote_headings("\n\n---\n\n".join(parts))[:60000])
            self.changes_title.show()
            self.changes.show()
        self.progress.hide()
        plan, update = self.plan, self.update
        lines = [tr("Версия {0} → {1}", self.state.get("version") or "?", plan.version or "?")]
        if (plan.game_version, plan.loader_version) != (self.state.get("game_version"), self.state.get("loader_version")):
            lines.append(tr("Minecraft {0} · {1} {2}", plan.game_version or "?",
                            LOADER_TITLES.get(plan.loader, plan.loader or "?"), plan.loader_version or ""))
        lines.append(tr("Скачается файлов: {0} · уберётся старых: {1} · обновится настроек: {2}",
                        len(update.download), len(update.remove), len(update.overrides)))
        if update.user_mods:
            lines.append(tr("Ваши моды останутся ({0}): {1}", len(update.user_mods),
                            ", ".join(update.user_mods[:5]) + ("…" if len(update.user_mods) > 5 else "")))
        if update.kept:
            lines.append(tr("Настройки, которые вы меняли, не трогаем ({0}): {1}", len(update.kept),
                            ", ".join(update.kept[:4]) + ("…" if len(update.kept) > 4 else "")))
        if plan.manual or plan.from_modrinth:
            lines.append(manual_summary(plan).strip())
        lines.append(tr("Всё заменённое уходит в бэкап — обновление можно отменить."))
        self.info.setText("\n".join(lines))
        self.update_btn.setEnabled(True)

    def _apply(self):
        self.accept()

    def _on_progress(self, done: int, total: int, text: str):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.progress_text.setText(tr("{0} из {1}: {2}", done, total, text))

    def _on_updated(self, backup):
        self.backup = backup
        self.accept()

    def _on_failed(self, error: str):
        if not error:
            super().reject()
            return
        self.progress.hide()
        text = tr("Не удалось: {0}", error)
        if self.worker.job == "update":
            text += "\n" + tr("Сборка осталась как была.")
        self.progress_text.setText(text)
        self.update_btn.setEnabled(self.plan is not None)

    def _cancel(self):
        if self.worker and self.worker.isRunning():
            self.progress_text.setText(tr("Отменяем…"))
            self.worker.stop()
            if self.worker.job == "prepare_update":
                self.worker.wait(3000)
                super().reject()
        else:
            super().reject()

    def reject(self):
        self._cancel()


class TransferDialog(FramelessDialog):
    """Перенос сборки в другой лаунчер. После accept(): kind (ключ лаунчера), name, keep (оставить копию)."""

    def __init__(self, parent, name: str, launchers: Dict[str, str], current_source: str):
        super().__init__(parent, tr("Перенести в другой лаунчер"), 520)
        self.launchers = launchers
        self.body.addWidget(label(tr("Создастся новая сборка с той же версией Minecraft и загрузчиком, и в неё "
                                     "переедут моды, настройки, миры, ресурспаки и шейдеры."), "Muted", wrap=True))
        self.body.addWidget(label(tr("Название"), "Faint"))
        self.name_edit = QLineEdit(name)
        self.body.addWidget(self.name_edit)
        self.group = QButtonGroup(self)
        self.radios: Dict[str, QRadioButton] = {}
        for kind, title in modpack_install.LAUNCHER_TITLES.items():
            if kind not in launchers or kind == current_source:
                continue
            text = title if kind != "curseforge" else tr("{0} — архив для импорта (старая сборка останется)", title)
            radio = QRadioButton(text)
            self.group.addButton(radio)
            self.body.addWidget(radio)
            self.radios[kind] = radio
        if self.radios:
            next(iter(self.radios.values())).setChecked(True)
        else:
            self.body.addWidget(label(tr("Других лаунчеров на компьютере не нашлось."), "Faint", wrap=True))
        self.keep = QCheckBox(tr("Оставить копию в старом месте"))
        self.body.addWidget(self.keep)
        self.hint = label(tr("Для Modrinth App закройте приложение на время переноса."), "Faint", wrap=True)
        self.body.addWidget(self.hint)
        self.body.addStretch()
        self.add_button(tr("Отмена"), False, self.reject)
        ok = self.add_button(tr("Перенести"), True, self.accept)
        ok.setEnabled(bool(self.radios))
        self.finish_layout()

    @property
    def kind(self) -> str:
        return next((k for k, r in self.radios.items() if r.isChecked()), "")

    @property
    def name(self) -> str:
        return self.name_edit.text().strip()


class ModpackRepairDialog(FramelessDialog):
    """Проверка сборки: сверка файлов с её выпуском. После accept(): plan и repair — что докачать (в фоне)."""

    def __init__(self, parent, state: Dict, game_dir: str, downloads: str):
        super().__init__(parent, tr("Проверка сборки"), 560)
        self.plan = None
        self.repair = None
        self.game_dir = game_dir
        self.title_text = state.get("title") or ""
        self.tokens = getattr(parent, "tokens", {})  # Для окна ручных файлов поверх этого
        self.worker: Optional[_Worker] = None
        self.body.addWidget(label(state.get("title") or "", "SectionTitle"))
        self.info = label(tr("Сверяем файлы сборки с её версией {0}…", state.get("version") or "?"), "Muted", wrap=True)
        self.body.addWidget(self.info)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.body.addWidget(self.progress)
        self.progress_text = label("", "Faint", wrap=True)
        self.body.addWidget(self.progress_text)
        self.details = QTextBrowser()
        self.details.setMinimumHeight(160)
        self.details.hide()
        self.body.addWidget(self.details)
        self.body.addStretch()
        self.pages_btn = self.add_button(tr("Открыть страницы"), False, self._open_pages)
        self.pages_btn.hide()
        self.cancel_btn = self.add_button(tr("Отмена"), False, self._cancel)
        self.fix_btn = self.add_button(tr("Докачать"), True, self.accept)
        self.fix_btn.setEnabled(False)
        self.finish_layout()
        self.worker = _Worker("prepare_repair", state.get("project_id") or "", downloads, game_dir=game_dir)
        self.worker.progress.connect(self._on_progress)
        self.worker.prepared.connect(self._on_checked)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, done: int, total: int, text: str):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.progress_text.setText(tr("{0} из {1}: {2}", done, total, text))

    def _on_checked(self, result):
        self.plan, self.repair = result
        repair = self.repair
        self.progress.hide()
        if repair.empty:
            self.info.setText(tr("Всё в порядке: файлов проверено — {0}, все на месте и целые.", repair.checked))
            self.progress_text.setText("")
            self.cancel_btn.setText(tr("Закрыть"))
            for widget in (self.details, self.pages_btn, self.fix_btn):
                widget.hide()
            return
        lines = [tr("Проверено файлов: {0}.", repair.checked)]
        if repair.missing:
            lines.append(tr("Не хватает: {0}", len(repair.missing)))
        if repair.broken:
            lines.append(tr("Испорчены (не совпала контрольная сумма): {0}", len(repair.broken)))
        if repair.overrides:
            lines.append(tr("Пропали настройки из сборки: {0}", len(repair.overrides)))
        if repair.prism_cfg:
            lines.append(tr("Настройки сборки в Prism неполные — допишем недостающие поля ({0}), заданные не тронем.",
                            len(repair.prism_cfg)))
        if self.plan.from_modrinth:
            lines.append(tr("Файлов «только с сайта», которые точно такими же лежат на Modrinth: {0} — скачаем "
                            "их оттуда.", self.plan.from_modrinth))
        if repair.manual:
            ready = len(manual_files.match_downloads(repair.manual, modpack_install.downloads_folder()))
            similar = sum(1 for f in repair.manual if f.path in self.plan.substitutes)
            lines.append(tr("Нет файлов, которые автор разрешает скачивать только с сайта: {0}. Скачайте их по "
                            "ссылкам в папку «Загрузки» — программа сама найдёт их и разложит по местам.",
                            len(repair.manual)))
            if similar:
                lines.append(tr("Для {0} из них есть похожие на Modrinth — можно скачать их.", similar))
            if ready:
                lines.append(tr("Уже лежат в «Загрузках»: {0}", ready))
            self.pages_btn.setText(tr("Скачать вручную ({0})…", len(repair.manual)))
            self.pages_btn.show()
        self.info.setText("\n".join(lines))
        self.progress_text.setText(tr("Ваши моды и изменённые вами настройки не трогаем; выключенные моды останутся "
                                      "выключенными."))
        names = [f"• {f.path}" for f in repair.missing] + [f"• {f.path} — " + tr("испорчен") for f in repair.broken]
        names += [f"• {r}" for r in repair.overrides] + [f"• {f.path} — " + tr("только с сайта") for f in repair.manual]
        self.details.setPlainText("\n".join(names[:500]))
        self.details.show()
        self.fix_btn.setEnabled(bool(repair.download or repair.overrides or repair.manual or repair.prism_cfg))

    def _open_pages(self):
        """Окно ручных файлов: страницы, слежение за «Загрузками», похожие файлы с Modrinth."""
        if not self.repair:
            return
        ManualFilesDialog(self, self.title_text, self.repair.manual, self.game_dir, self.plan.substitutes).exec()
        self.repair.manual = manual_files.still_missing(self.repair.manual, self.game_dir)
        self._on_checked((self.plan, self.repair))

    def _on_failed(self, error: str):
        if not error:
            super().reject()
            return
        self.progress.hide()
        self.progress_text.setText(tr("Не удалось проверить: {0}", error))

    def _cancel(self):
        if self.worker and self.worker.isRunning():
            self.progress_text.setText(tr("Отменяем…"))
            self.worker.stop()
            self.worker.wait(3000)
        super().reject()

    def reject(self):
        self._cancel()



class ManualFilesDialog(FramelessDialog):
    """
    Файлы «только с сайта», которых нет в сборке. Пока окно открыто, программа следит за папкой «Загрузки»:
    скачанный браузером файл (даже переименованный) сверяется и сам ложится в сборку. Если на Modrinth есть
    похожий файл, можно скачать его оттуда — с честной пометкой, что он не тот же самый.
    """
    SCAN_MS = 2000

    def __init__(self, parent, title: str, items: list, game_dir: str, substitutes: Optional[Dict] = None):
        super().__init__(parent, tr("Файлы, которые нужно скачать вручную"), 640)
        self.game_dir = game_dir
        self.items = list(items)
        self.substitutes = substitutes or {}
        self.since = manual_files.downloads_since()
        self.workers = []
        self.tokens = getattr(parent, "tokens", {}) or {}
        self.ready = set()  # Пути файлов, которые уже в сборке
        self.body.addWidget(label(title, "SectionTitle"))
        self.body.addWidget(label(tr("Автор разрешает скачивать эти файлы только с сайта CurseForge. Откройте страницы "
                                     "и скачайте файлы браузером в папку «Загрузки» — программа сама заметит их и "
                                     "положит в сборку. Окно можно не закрывать."), "Muted", wrap=True))
        self.summary = label("", "Faint", wrap=True)
        self.body.addWidget(self.summary)
        rows = QWidget()
        self.grid = QGridLayout(rows)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(8)
        self.status: Dict[str, object] = {}
        self.similar_btns: Dict[str, QPushButton] = {}
        for row, item in enumerate(self.items):
            name = label(item.name or os.path.basename(item.path), None, wrap=True)
            name.setToolTip(item.path)
            self.grid.addWidget(name, row, 0)
            status = label(tr("ждём в «Загрузках»"), "Faint")
            self.grid.addWidget(status, row, 1)
            self.status[item.path] = status
            page = QPushButton(tr("Страница"))
            page.setEnabled(bool(item.manual_url))
            page.clicked.connect(lambda _=False, url=item.manual_url: self._open(url))
            self.grid.addWidget(page, row, 2)
            sub = self.substitutes.get(item.path)
            if sub:
                similar = QPushButton(tr("С Modrinth…"))
                similar.setToolTip(tr("На Modrinth есть похожий файл: {0} {1}. Это не тот же самый файл, что в сборке "
                                      "(другая сборка мода) — обычно подходит, но может отличаться.",
                                      sub.title, sub.version))
                similar.clicked.connect(lambda _=False, it=item: self._from_modrinth(it))
                self.grid.addWidget(similar, row, 3)
                self.similar_btns[item.path] = similar
        self.grid.setColumnStretch(0, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(rows)
        scroll.setMinimumHeight(min(360, 44 * max(1, len(self.items)) + 8))
        self.body.addWidget(scroll, 1)
        self.add_button(tr("Открыть все страницы"), False, self._open_all)
        self.add_button(tr("Открыть «Загрузки»"), False,
                        lambda: self._open_folder(modpack_install.downloads_folder()))
        self.close_btn = self.add_button(tr("Готово"), True, self.accept)
        self.finish_layout()
        self.timer = QTimer(self, interval=self.SCAN_MS)
        self.timer.timeout.connect(self._scan)
        self.timer.start()
        self._scan()

    @staticmethod
    def _open(url: str):
        import webbrowser
        if url:
            webbrowser.open(url)

    def _open_all(self):
        for item in manual_files.still_missing(self.items, self.game_dir):
            self._open(item.manual_url)

    @staticmethod
    def _open_folder(path: str):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _mark(self, path: str, text: str):
        self.ready.add(path)
        self.status[path].setText(text)
        self.status[path].setStyleSheet(f"color: {self.tokens.get('success', '#3ecf8e')}; font-weight: 600;")
        if path in self.similar_btns:
            self.similar_btns[path].setEnabled(False)

    def _scan(self):
        """Раз в пару секунд: что из нужного появилось в «Загрузках» — кладём в сборку."""
        missing = manual_files.still_missing(self.items, self.game_dir)
        for item in self.items:
            if item not in missing and item.path not in self.ready:
                self._mark(item.path, tr("✓ на месте"))
        if missing:
            try:
                for item in modpack_install.pick_manual_downloads(missing, self.game_dir, since=self.since):
                    self._mark(item.path, tr("✓ взят из «Загрузок»"))
            except OSError as e:
                logging.warning(tr("Сборка: не удалось взять файл из «Загрузок»: {0}", e))
        left = len(manual_files.still_missing(self.items, self.game_dir))
        if left:
            self.summary.setText(tr("Осталось скачать: {0} из {1}.", left, len(self.items)))
        else:
            self.summary.setText(tr("Все файлы на месте — сборка готова."))
            self.timer.stop()

    def _from_modrinth(self, item):
        from workers import TaskWorker
        sub = self.substitutes[item.path]
        if not confirm(self, tr("Похожий файл с Modrinth"),
                       tr("Скачать «{0}» {1} с Modrinth вместо файла из сборки?\n\nЭто не тот же самый файл: автор "
                          "сборки выбрал другую его сборку. Обычно такой файл подходит, но если игра поведёт себя "
                          "странно — скачайте оригинал с CurseForge.", sub.title, sub.version),
                       tr("Скачать с Modrinth"), tr("Отмена")):
            return
        self.similar_btns[item.path].setEnabled(False)
        self.status[item.path].setText(tr("качаем с Modrinth…"))
        worker = TaskWorker(manual_files.download_similar, item, sub, self.game_dir)
        self.workers.append(worker)
        worker.succeeded.connect(lambda _: (self._mark(item.path, tr("✓ с Modrinth (похожий)")), self._scan()))
        worker.failed.connect(lambda error: (self.status[item.path].setText(tr("не скачался: {0}", error)),
                                             self.similar_btns[item.path].setEnabled(True)))
        worker.start()

    def done(self, result):
        self.timer.stop()
        for worker in self.workers:
            worker.wait(5000)
        super().done(result)
