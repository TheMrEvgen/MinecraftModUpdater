"""
Учёт загрузок для индикатора в боковой панели и страницы «Загрузки» (без Qt).

Загрузка (DownloadJob) — одна операция: обновление модов, установка или обновление сборки. Потоки, которые
качают, сообщают о каждом файле (file_started / file_progress / file_done); окно несколько раз в секунду
читает состояние (snapshot) и само считает скорость и оставшееся время. Все методы потокобезопасны.

Управление: pause()/resume() — потоки ждут в wait_if_paused() (и докачивают файл с того же места);
cancel(); retry() — снова скачать файлы с ошибкой (если операция умеет: retry_callback). Загрузка в очереди
(WAITING) ещё не началась — окно запускает её, когда закончится предыдущая.
"""
import collections
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

QUEUED, ACTIVE, DONE, FAILED, CANCELLED = "queued", "active", "done", "failed", "cancelled"
WAITING = "waiting"  # Вся загрузка ждёт своей очереди
SPEED_WINDOW = 4.0      # Скорость — по последним секундам: видно, если загрузка встала
HISTORY_SECONDS = 90    # Сколько секунд держим для графика скорости
KEEP_FINISHED = 20      # Завершённых загрузок в списке


@dataclass
class DownloadFile:
    key: str
    name: str
    size: int = 0
    done: int = 0
    state: str = QUEUED
    error: str = ""


class DownloadJob:
    def __init__(self, title: str, kind: str = "mods", cancel: Optional[Callable[[], None]] = None):
        self.title = title
        self.kind = kind                # mods — моды сборки, modpack — готовая сборка
        self.cancel_callback = cancel
        self.retry_callback: Optional[Callable[[], None]] = None  # Докачать файлы с ошибкой
        self.files: Dict[str, DownloadFile] = {}
        self.state = ACTIVE
        self._resume = threading.Event()
        self._resume.set()
        self.started = time.time()
        self.finished: Optional[float] = None
        self.summary = ""               # Итог для списка завершённых: «Установлено 51 файл» и т.п.
        self._lock = threading.Lock()
        self._samples = collections.deque()   # (время, скачано байт) для скорости
        self.history: List[float] = []        # Скорость по секундам — для графика
        self._last_second = 0

    # --- Сообщения от потоков, которые качают ---

    def add_file(self, key: str, name: str, size: int = 0):
        with self._lock:
            self.files[key] = DownloadFile(key, name, max(0, int(size or 0)))

    def file_started(self, key: str):
        with self._lock:
            if key in self.files:
                self.files[key].state = ACTIVE

    def file_progress(self, key: str, done: int, total: int = 0):
        with self._lock:
            item = self.files.get(key)
            if item:
                item.state = ACTIVE
                item.done = done
                if total and total > 0:
                    item.size = total

    def file_done(self, key: str, error: str = "", cancelled: bool = False):
        with self._lock:
            item = self.files.get(key)
            if item:
                item.state = CANCELLED if cancelled else FAILED if error else DONE
                item.error = error
                if item.state == DONE:
                    item.done = max(item.done, item.size)

    def finish(self, state: str = DONE, summary: str = ""):
        with self._lock:
            if self.finished is not None:
                return
            self.state = state
            self.summary = summary
            self.finished = time.time()
            for item in self.files.values():
                if item.state in (QUEUED, ACTIVE):
                    item.state = CANCELLED if state == CANCELLED else item.state

    def cancel(self):
        if self.cancel_callback and self.finished is None:
            self._resume.set()  # Поток на паузе должен проснуться, чтобы увидеть отмену
            self.cancel_callback()

    # --- Пауза ---

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self):
        if self.finished is None:
            self._resume.clear()

    def resume(self):
        self._resume.set()

    def wait_if_paused(self, cancelled: Callable[[], bool] = lambda: False) -> bool:
        """Для качающих потоков: ждёт, пока сняли с паузы. True — пауза была (соединение стоит открыть заново)."""
        was_paused = self.paused
        while not self._resume.wait(0.2):
            if cancelled():
                break
        return was_paused

    # --- Очередь и повтор ---

    def wait_in_queue(self):
        self.state = WAITING

    def start_now(self):
        if self.state == WAITING:
            self.state = ACTIVE
            self.started = time.time()

    @property
    def can_retry(self) -> bool:
        return bool(self.retry_callback) and self.finished is not None and any(
            f.state in (FAILED, CANCELLED) for f in self.files.values())

    def restart(self, keys: List[str]):
        """Повтор: файлы keys снова в очереди, загрузка снова идёт (окно запускает докачку)."""
        with self._lock:
            for key in keys:
                if key in self.files:
                    self.files[key].state, self.files[key].error, self.files[key].done = QUEUED, "", 0
            self.state, self.finished, self.summary = ACTIVE, None, ""
            self._samples.clear()
        self._resume.set()

    def retry(self):
        if self.can_retry:
            self.retry_callback()

    def failed_keys(self) -> List[str]:
        with self._lock:
            return [f.key for f in self.files.values() if f.state in (FAILED, CANCELLED)]

    # --- Чтение для окна ---

    def totals(self) -> Dict[str, int]:
        with self._lock:
            files = list(self.files.values())
        return {"bytes": sum(f.done for f in files), "size": sum(max(f.size, f.done) for f in files),
                "files": len(files), "files_done": sum(1 for f in files if f.state == DONE),
                "failed": sum(1 for f in files if f.state == FAILED),
                "active": sum(1 for f in files if f.state == ACTIVE)}

    def sample(self, now: Optional[float] = None):
        """Запоминает, сколько скачано к этому моменту (окно вызывает несколько раз в секунду)."""
        now = now or time.monotonic()
        done = self.totals()["bytes"]
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > SPEED_WINDOW:
            self._samples.popleft()
        second = int(now)
        if second != self._last_second and self.finished is None:
            self._last_second = second
            self.history.append(self.speed())
            del self.history[:-HISTORY_SECONDS]

    def speed(self) -> float:
        """Байт в секунду за последние секунды."""
        if len(self._samples) < 2 or self.finished is not None or self.paused or self.state == WAITING:
            return 0.0
        (start, first), (end, last) = self._samples[0], self._samples[-1]
        return (last - first) / (end - start) if end > start else 0.0

    def eta(self) -> Optional[float]:
        """Сколько секунд осталось или None, если пока не понять (скорость ноль или размер неизвестен)."""
        totals = self.totals()
        speed = self.speed()
        if self.finished is not None or speed <= 0 or not totals["size"]:
            return None
        return max(0.0, (totals["size"] - totals["bytes"]) / speed)

    def fraction(self) -> float:
        totals = self.totals()
        if totals["size"]:
            return min(1.0, totals["bytes"] / totals["size"])
        return totals["files_done"] / totals["files"] if totals["files"] else 0.0

    def snapshot_files(self) -> List[DownloadFile]:
        """Файлы для списка: сначала те, что качаются, потом ошибки, очередь и готовые."""
        order = {ACTIVE: 0, FAILED: 1, QUEUED: 2, CANCELLED: 3, DONE: 4}
        with self._lock:
            files = [DownloadFile(**vars(f)) for f in self.files.values()]
        return sorted(files, key=lambda f: order.get(f.state, 5))


class DownloadManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.jobs: List[DownloadJob] = []

    def start(self, title: str, kind: str = "mods", cancel: Optional[Callable[[], None]] = None) -> DownloadJob:
        job = DownloadJob(title, kind, cancel)
        with self._lock:
            self.jobs.append(job)  # В конец очереди
            finished = sorted((j for j in self.jobs if j.finished is not None), key=lambda j: -j.finished)
            for old in finished[KEEP_FINISHED:]:
                self.jobs.remove(old)
        return job

    def move(self, job: DownloadJob, delta: int):
        """Сдвиг в очереди среди ждущих: -1 — начнётся раньше, +1 — позже."""
        with self._lock:
            waiting = [j for j in self.jobs if j.state == WAITING]
            if job not in waiting:
                return
            i = waiting.index(job)
            other = waiting[max(0, min(len(waiting) - 1, i + delta))]
            a, b = self.jobs.index(job), self.jobs.index(other)
            self.jobs[a], self.jobs[b] = self.jobs[b], self.jobs[a]

    def active(self) -> List[DownloadJob]:
        with self._lock:
            return [j for j in self.jobs if j.finished is None]

    def all(self) -> List[DownloadJob]:
        """Для показа: идущие и ждущие — в порядке очереди (выше — раньше), затем завершённые, свежие сверху."""
        with self._lock:
            jobs = list(self.jobs)
        running = [j for j in jobs if j.finished is None]
        return running + sorted((j for j in jobs if j.finished is not None), key=lambda j: -j.finished)

    def clear_finished(self):
        with self._lock:
            self.jobs = [j for j in self.jobs if j.finished is None]


MANAGER = DownloadManager()


def status_text(job: "DownloadJob") -> Optional[str]:
    """Особое состояние вместо времени: на паузе или ждёт очереди."""
    from i18n import tr
    if job.state == WAITING:
        return tr("ждёт очереди")
    if job.paused and job.finished is None:
        return tr("на паузе")
    return None


def format_eta(seconds: Optional[float]) -> str:
    from i18n import tr
    if seconds is None:
        return tr("оцениваем…")
    if seconds < 60:
        return tr("меньше минуты")
    minutes = round(seconds / 60)
    if minutes < 60:
        return tr("~{0} мин", minutes)
    return tr("~{0} ч {1} мин", minutes // 60, minutes % 60)
