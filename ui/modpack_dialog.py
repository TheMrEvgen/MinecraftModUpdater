"""Окна готовой сборки из каталога: установка (что внутри, куда ставить) и обновление до новой версии."""
import logging
from typing import Dict, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QFileDialog, QHBoxLayout, QLineEdit, QProgressBar, QRadioButton

import modpack_install
from i18n import tr
from ui.dialogs import FramelessDialog
from ui.widgets import button, label
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
                path = modpack_install.download_pack(info, self.folder)
                self.prepared.emit(modpack_install.load_plan(path))
            elif self.job == "install":
                errors = modpack_install.install(self.plan, self.target, self.progress.emit, lambda: self._cancelled,
                                                 job=self.download)
                try:
                    modpack_install.save_state(self.plan, self.project_id, self.target.game_dir, self.title)
                except OSError as e:  # Без записи сборка работает, только не обновится из каталога
                    logging.warning(tr("Сборка: не удалось запомнить состав: {0}", e))
                self.installed.emit(errors)
            elif self.job == "prepare_update":
                info = modpack_install.latest_pack_file(self.project_id)
                plan = modpack_install.load_plan(modpack_install.download_pack(info, self.folder))
                state = modpack_install.load_state(self.game_dir) or {}
                self.prepared.emit((plan, modpack_install.plan_update(state, plan, self.game_dir)))
            else:
                self.installed.emit(modpack_install.apply_update(self.update, self.plan, self.game_dir,
                                                                 self.progress.emit, lambda: self._cancelled,
                                                                 job=self.download))
        except modpack_install.InstallCancelled:
            self.failed.emit("")
        except Exception as e:
            self.failed.emit(str(e))


class ModpackInstallDialog(FramelessDialog):
    """После accept(): plan и target — что и куда ставить. Сама установка идёт в фоне (окно её запускает)."""

    def __init__(self, parent, hit: Dict, prism_instances: Optional[str], default_folder: str, downloads: str,
                 minecraft_dir: Optional[str] = None):
        super().__init__(parent, tr("Установка сборки"), 560)
        self.hit = hit
        self.prism_instances = prism_instances
        self.minecraft_dir = minecraft_dir
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
        self.where = QButtonGroup(self)
        self.prism_radio = QRadioButton(tr("Новая сборка в Prism Launcher — «Играть» работает сразу"))
        self.official_radio = QRadioButton(tr("Новая установка в Minecraft Launcher — со своей папкой игры"))
        self.folder_radio = QRadioButton(tr("Папка на диске (запустить в своём лаунчере)"))
        self.where.addButton(self.prism_radio)
        self.where.addButton(self.official_radio)
        self.where.addButton(self.folder_radio)
        self.prism_radio.setVisible(bool(prism_instances))
        self.official_radio.setVisible(False)  # Только для Fabric/Quilt — станет ясно, когда прочитаем сборку
        (self.prism_radio if prism_instances else self.folder_radio).setChecked(True)
        self.body.addWidget(self.prism_radio)
        self.body.addWidget(self.official_radio)
        self.body.addWidget(self.folder_radio)
        row = QHBoxLayout()
        self.folder_edit = QLineEdit(default_folder)
        browse = button(tr("Обзор…"), None, "folder", parent.tokens, "text")
        browse.clicked.connect(self._browse)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        self.body.addLayout(row)
        self.folder_hint = label(tr("Внутри появится папка сборки. Загрузчик ({0}) поставьте в лаунчере сами, указав эту "
                                    "папку как папку игры.", "Fabric"), "Faint", wrap=True)
        self.body.addWidget(self.folder_hint)
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

    def _refresh_where(self):
        folder = self.folder_radio.isChecked()
        self.folder_edit.setEnabled(folder)
        self.folder_hint.setVisible(folder)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, tr("Куда поставить сборку"), self.folder_edit.text())
        if path:
            self.folder_edit.setText(path)

    def _on_prepared(self, plan):
        self.plan = plan
        self.progress.hide()
        loader = LOADER_TITLES.get(plan.loader, plan.loader or "?")
        text = tr("Minecraft {0} · {1} {2} · файлов: {3}", plan.game_version or "?", loader, plan.loader_version or "",
                  len(plan.files))
        if plan.version:
            text = tr("Версия сборки {0}. ", plan.version) + text
        if plan.manual:
            text += "\n" + tr("{0} — автор разрешает скачивать только с сайта; ссылки покажем после установки.",
                              ", ".join(f.name for f in plan.manual[:4]))
        self.info.setText(text)
        self.folder_hint.setText(tr("Внутри появится папка сборки. Загрузчик ({0} {1}) поставьте в лаунчере сами, указав "
                                    "эту папку как папку игры.", loader, plan.loader_version or ""))
        import official_launcher
        official = bool(self.minecraft_dir and official_launcher.can_install(plan.loader) and plan.loader_version)
        self.official_radio.setVisible(official)
        if official and not self.prism_instances:
            self.official_radio.setChecked(True)
        self.install_btn.setEnabled(bool(plan.game_version))

    def _install(self):
        name = self.name_edit.text().strip() or self.plan.name
        if self.prism_radio.isChecked() and self.prism_instances:
            self.target = modpack_install.prism_target(self.prism_instances, name)
        elif self.official_radio.isChecked() and self.minecraft_dir:
            self.target = modpack_install.official_target(self.minecraft_dir, name)
        else:
            self.target = modpack_install.folder_target(self.folder_edit.text().strip(), name)
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
        super().__init__(parent, tr("Обновление сборки"), 560)
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
        self.plan, self.update = result
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
        if plan.manual:
            lines.append(tr("{0} — автор разрешает скачивать только с сайта; ссылки покажем после обновления.",
                            ", ".join(f.name for f in plan.manual[:4])))
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
