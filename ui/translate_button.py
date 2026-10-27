"""Кнопка «Перевести» / «Показать оригинал» для описаний и списков изменений (перевод — в translate.py)."""
from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QPushButton

import icons
from i18n import language, tr

Source = Tuple[str, Callable[[str], None]]  # (оригинал, как показать текст)


class TranslateButton(QPushButton):
    """Переводит набор текстов на язык интерфейса. Выбор «переводить» общий для всех кнопок до конца сеанса,
    а с настройкой «Переводить автоматически» включён сразу."""
    auto = False         # Настройка «Переводить описания автоматически»
    wanted = False       # Пользователь нажал «Перевести» — переводим и следующие описания

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.tokens = tokens
        self.setObjectName("GhostButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIcon(icons.icon("globe", tokens["text_muted"], 14))
        self.setProperty("icon_name", "globe")
        self.setProperty("icon_color_key", "text_muted")
        self.clicked.connect(self._toggle)
        self.sources: List[Source] = []
        self.translated = False
        self._generation = 0
        self._workers = []
        self._update_text()
        self.hide()

    def set_sources(self, sources: List[Source]):
        """Новые тексты (другой мод): показываем оригинал, а если перевод включён — сразу переводим."""
        self._generation += 1
        self.sources = [(text or "", apply) for text, apply in sources]
        self.translated = False
        self.setVisible(any(text.strip() for text, _ in self.sources))
        self._update_text()
        if self.isVisible() and (TranslateButton.auto or TranslateButton.wanted):
            self._translate()

    def _toggle(self):
        if self.translated or self._busy():
            TranslateButton.wanted = False
            self._generation += 1
            self.translated = False
            for text, apply in self.sources:
                apply(text)
            self._update_text()
        else:
            TranslateButton.wanted = True
            self._translate()

    def _busy(self) -> bool:
        return any(w.isRunning() for w in self._workers)

    def _translate(self):
        import translate
        from workers import TaskWorker
        generation, texts = self._generation, [text for text, _ in self.sources]
        worker = TaskWorker(translate.translate_many, texts, language())
        self._workers.append(worker)

        def done(result: Optional[list]):
            if generation != self._generation or not result:
                return
            for (_, apply), text in zip(self.sources, result):
                apply(text)
            self.translated = True
            self._update_text()

        def failed(error: str):
            if generation == self._generation:
                self.setText(tr("Не удалось перевести"))
                self.setToolTip(error)

        worker.succeeded.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        worker.start()
        self.setText(tr("Переводим…"))

    def _update_text(self):
        self.setText(tr("Показать оригинал") if self.translated else tr("Перевести"))
        self.setToolTip(tr("Перевод Google Переводчиком на язык интерфейса"))
