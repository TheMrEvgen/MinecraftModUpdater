"""Загрузки: индикатор в боковой панели и страница «Загрузки» (ui/downloads.py), учёт — downloads.MANAGER.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import time
from typing import Callable, Optional

from PyQt6.QtCore import QTimer

import downloads
from ui.downloads import DONE_VISIBLE_SECONDS, DownloadIndicator, DownloadsPage
from window.common import PAGE_DOWNLOADS

TICK_MS = 250


class DownloadsMixin:
    def _setup_downloads(self):
        """Вызывается при построении окна: индикатор встаёт в боковую панель, страница — в стопку страниц."""
        self.download_indicator = DownloadIndicator(self.tokens)
        self.download_indicator.clicked.connect(lambda: self._switch_page(PAGE_DOWNLOADS))
        self.sidebar.set_download_indicator(self.download_indicator)
        self.downloads_page = DownloadsPage(self.tokens)
        self.stack.addWidget(self.downloads_page)
        self._downloads_timer = QTimer(self, interval=TICK_MS)
        self._downloads_timer.timeout.connect(self._downloads_tick)

    def _start_download(self, title: str, kind: str = "mods",
                        cancel: Optional[Callable[[], None]] = None) -> downloads.DownloadJob:
        job = downloads.MANAGER.start(title, kind, cancel)
        if not self._downloads_timer.isActive():
            self._downloads_timer.start()
        self._downloads_tick()
        return job

    def _downloads_tick(self):
        jobs = downloads.MANAGER.all()
        now = time.monotonic()
        for job in jobs:
            if job.finished is None:
                job.sample(now)
        self.download_indicator.refresh(jobs)
        if self.stack.currentIndex() == PAGE_DOWNLOADS:
            self.downloads_page.refresh()
        recent = any(j.finished is not None and time.time() - j.finished < DONE_VISIBLE_SECONDS for j in jobs)
        if not any(j.finished is None for j in jobs) and not recent:
            self._downloads_timer.stop()  # Всё скачано и «Готово» уже показали — не тратим время
