"""Запуск игры: команда для лаунчера и состояние запуска по логу."""
import os
import time


def test_launch_plan_prism(tmp_path, monkeypatch):
    import game_runner
    instance = tmp_path / "PrismLauncher" / "instances" / "My Pack"
    (instance / "minecraft").mkdir(parents=True)
    exe = tmp_path / "PrismLauncher" / "prismlauncher.exe"  # Портативный Prism: exe рядом с данными
    exe.write_bytes(b"MZ")
    plan = game_runner.launch_plan({"source": "prism", "mods_path": str(instance / "minecraft")})
    assert plan.command == [str(exe), "--launch", "My Pack"] and plan.opens_instance


def test_launch_plan_custom_command():
    import game_runner
    plan = game_runner.launch_plan({"launch_command": '"C:\\Games\\launcher.exe" --profile Survival'})
    assert plan.command == ["C:\\Games\\launcher.exe", "--profile", "Survival"] and plan.opens_instance


def test_run_state(tmp_path):
    import game_runner
    since = time.time() - 5
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "latest.log"
    log.write_text("[main/INFO]: Loading\n[Render thread/INFO]: Sound engine started\n", encoding="utf-8")
    state = game_runner.run_state(str(tmp_path), since)
    assert state.log_seen and state.reached_menu and not state.crashed

    log.write_text("[main/ERROR]: Incompatible mods found!\n", encoding="utf-8")
    state = game_runner.run_state(str(tmp_path), since)
    assert state.crashed and "Incompatible mods found" in state.reason

    old = time.time() - 3600
    os.utime(log, (old, old))  # Лог прошлого запуска не учитываем
    assert not game_runner.run_state(str(tmp_path), since).log_seen
    (tmp_path / "crash-reports").mkdir()
    (tmp_path / "crash-reports" / "crash-1.txt").write_text("---- Minecraft Crash Report ----")
    state = game_runner.run_state(str(tmp_path), since)
    assert state.crashed and state.crash_file.endswith("crash-1.txt")
