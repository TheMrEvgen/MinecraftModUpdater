"""История запусков: время загрузки по логу, статистика, какая операция с модами сломала запуск."""
import datetime

import launch_history
from game_runner import load_seconds_from_log


def at(hour, minute=0):
    return datetime.datetime(2026, 10, 23, hour, minute).timestamp()


def run(hour, ok=True, load=30.0):
    return {"start": at(hour), "menu": ok, "crashed": not ok, "load": load if ok else None}


def operation(hour, kind="update", installed=("sodium-0.6.jar",)):
    return {"created": datetime.datetime(2026, 10, 23, hour, 30).isoformat(timespec="seconds"), "kind": kind,
            "installed": list(installed), "dir": f"tx{hour}"}


def test_load_time_from_log_formats():
    fabric = "[19:49:29] [main/INFO]: Loading Minecraft\n[19:49:31] [main/INFO]: x\n[19:50:05] [Render thread/INFO]: Sound engine started\n"
    assert load_seconds_from_log(fabric) == 36.0
    neoforge = ("[30янв.2026 19:49:29.978] [main/INFO] [FMLLoader/]: start\n"
                "[30янв.2026 19:49:44.139] [Render thread/INFO] [SoundEngine/SOUNDS]: Sound engine started\n")
    assert load_seconds_from_log(neoforge) == 14.2
    assert load_seconds_from_log("[23:59:50] [main/INFO]: a\n[00:00:20] [Render thread/INFO]: Sound engine started") == 30.0
    assert load_seconds_from_log("[10:00:00] [main/INFO]: crashed before menu") is None
    assert load_seconds_from_log("") is None


def test_record_and_stats():
    history = []
    for i in range(60):
        history = launch_history.record(history, {"start": i, "menu": True, "crashed": False, "load": float(i)})
    assert len(history) == launch_history.MAX_RUNS and history[0]["start"] == 10
    history = launch_history.record(history, {"start": 100, "menu": False, "crashed": True, "load": None})
    stats = launch_history.stats(history)
    assert stats["runs"] == 50 and stats["crashes"] == 1
    assert stats["last_load"] == 59.0 and stats["average_load"] == sum(range(50, 60)) / 10
    assert launch_history.stats([])["average_load"] is None


def test_suspect_operation():
    transactions = [operation(15), operation(13), operation(11)]  # Новые первыми, как list_transactions
    # Удачно в 12:00, обновление в 13:30, вылеты в 14:00 и 16:00 → виновата операция 13:30 (а не 15:30, она позже вылета)
    history = [run(10), run(12), run(14, ok=False), run(16, ok=False)]
    assert launch_history.suspect_operation(history, transactions)["dir"] == "tx13"
    # Последний запуск удачный — подсказывать нечего
    assert launch_history.suspect_operation(history + [run(17)], transactions) is None
    # Удачных запусков не было вовсе — сравнивать не с чем
    assert launch_history.suspect_operation([run(14, ok=False)], transactions) is None
    # Между удачным запуском и вылетом модов не меняли — вылет не из-за обновления
    assert launch_history.suspect_operation([run(13, ok=True), run(14, ok=False)],
                                            [operation(11)]) is None


def test_changed_mods():
    manifest = {"installed": ["a.jar", "b.jar", "c.jar", "d.jar", "e.jar", "f.jar"]}
    assert launch_history.changed_mods(manifest) == "a, b, c, d (+2)"
    assert launch_history.changed_mods({"backed_up": [{"name": "old.jar", "backup": "old.jar.1"}]}) == "old"
