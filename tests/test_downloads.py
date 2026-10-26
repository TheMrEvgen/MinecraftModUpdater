"""Учёт загрузок: прогресс, скорость, оставшееся время, порядок файлов, отмена и список завершённых."""
import downloads


def test_progress_speed_and_eta():
    manager = downloads.DownloadManager()
    job = manager.start("Сборка", "modpack")
    job.add_file("a", "A", 1000)
    job.add_file("b", "B", 3000)
    job.sample(100.0)
    job.file_progress("a", 1000, 1000)
    job.file_done("a")
    job.file_progress("b", 1000, 3000)
    job.sample(102.0)
    totals = job.totals()
    assert (totals["bytes"], totals["size"], totals["files_done"], totals["active"]) == (2000, 4000, 1, 1)
    assert job.speed() == 1000.0 and job.eta() == 2.0 and job.fraction() == 0.5
    assert [f.key for f in job.snapshot_files()] == ["b", "a"]  # Качающийся — выше готового
    assert job.history  # Точка для графика за новую секунду


def test_finish_cancel_and_cleanup():
    manager = downloads.DownloadManager()
    cancelled = []
    job = manager.start("Моды", cancel=lambda: cancelled.append(True))
    job.add_file("x", "X", 10)
    job.cancel()
    job.finish(downloads.CANCELLED, "Отменено")
    assert cancelled == [True] and job.files["x"].state == downloads.CANCELLED
    assert job.speed() == 0 and job.eta() is None and manager.active() == []
    job.cancel()  # После завершения отмена ничего не делает
    assert cancelled == [True]
    running = manager.start("Ещё")
    manager.clear_finished()
    assert manager.all() == [running]


def test_unknown_size_and_eta_text():
    job = downloads.DownloadJob("Мод")
    job.add_file("m", "M")
    job.file_progress("m", 500)
    assert job.totals()["size"] == 500 and job.eta() is None
    assert downloads.format_eta(None) and downloads.format_eta(30) != downloads.format_eta(600)
