"""Окно установки готовой сборки из каталога: что внутри, куда ставить, загрузка с прогрессом."""
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
    """Шаг 1 (prepare): скачать файл сборки и прочитать её. Шаг 2 (install): поставить в выбранное место."""
    progress = pyqtSignal(int, int, str)
    prepared = pyqtSignal(object)
    installed = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, job: str, project_id: str = "", folder: str = "", plan=None, target=None):
        super().__init__()
        self.job, self.project_id, self.folder, self.plan, self.target = job, project_id, folder, plan, target
        self._cancelled = False

    def stop(self):
        self._cancelled = True

    def run(self):
        try:
            if self.job == "prepare":
                info = modpack_install.latest_pack_file(self.project_id)
                path = modpack_install.download_pack(info, self.folder)
                self.prepared.emit(modpack_install.load_plan(path))
            else:
                errors = modpack_install.install(self.plan, self.target, self.progress.emit, lambda: self._cancelled)
                self.installed.emit(errors)
        except modpack_install.InstallCancelled:
            self.failed.emit("")
        except Exception as e:
            self.failed.emit(str(e))


class ModpackInstallDialog(FramelessDialog):
    """После accept(): plan, target, errors — сборка установлена."""

    def __init__(self, parent, hit: Dict, prism_instances: Optional[str], default_folder: str, downloads: str):
        super().__init__(parent, tr("Установка сборки"), 560)
        self.hit = hit
        self.prism_instances = prism_instances
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
        self.folder_radio = QRadioButton(tr("Папка на диске (запустить в своём лаунчере)"))
        self.where.addButton(self.prism_radio)
        self.where.addButton(self.folder_radio)
        self.prism_radio.setVisible(bool(prism_instances))
        (self.prism_radio if prism_instances else self.folder_radio).setChecked(True)
        self.body.addWidget(self.prism_radio)
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
        self.install_btn.setEnabled(bool(plan.game_version))

    def _install(self):
        name = self.name_edit.text().strip() or self.plan.name
        if self.prism_radio.isChecked() and self.prism_instances:
            self.target = modpack_install.prism_target(self.prism_instances, name)
        else:
            self.target = modpack_install.folder_target(self.folder_edit.text().strip(), name)
        for widget in (self.name_edit, self.prism_radio, self.folder_radio, self.folder_edit, self.install_btn):
            widget.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self._start(_Worker("install", plan=self.plan, target=self.target))

    def _on_progress(self, done: int, total: int, text: str):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.progress_text.setText(tr("{0} из {1}: {2}", done, total, text))

    def _on_installed(self, errors: list):
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
