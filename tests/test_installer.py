"""Установка в профиль пользователя: файлы, ярлыки и записи реестра (в отдельном тестовом разделе реестра)."""
import os
import shutil
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="только Windows")
TEST_ROOT = r"Software\ModUpdaterTest"


@pytest.fixture
def paths(tmp_path):
    import installer
    yield installer.InstallPaths(str(tmp_path / "install"), str(tmp_path / "start"), str(tmp_path / "desktop"), TEST_ROOT)
    installer._delete_tree(TEST_ROOT)


def test_install_and_uninstall(tmp_path, paths):
    import installer
    exe = tmp_path / "source.exe"
    shutil.copy(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), exe)  # Настоящий .exe — со значком
    done = installer.install(str(exe), desktop=True, paths=paths)
    assert os.path.isfile(paths.exe) and os.path.isfile(paths.start_shortcut) and os.path.isfile(paths.desktop_shortcut)
    assert any(".mrpack" in step for step in done)
    state = installer.state(paths)
    assert state.installed and state.mrpack and state.protocol and state.desktop
    keys = installer._keys(paths)
    assert installer._read_value(keys["protocol"] + r"\shell\open\command") == f'"{paths.exe}" "%1"'
    assert installer._read_value(keys["uninstall"], "UninstallString") == f'"{paths.exe}" --uninstall'

    installer.uninstall(paths, schedule_exe_removal=False)
    state = installer.state(paths)
    assert not state.installed and not state.mrpack and not state.protocol
    assert not os.path.exists(paths.start_shortcut) and not os.path.exists(paths.desktop_shortcut)


def test_does_not_steal_mrpack_default(tmp_path, paths):
    import installer
    keys = installer._keys(paths)
    installer._set_values(keys["mrpack_default"], {"": "ModrinthApp.mrpack"})
    exe = tmp_path / "source.exe"
    exe.write_bytes(b"MZ")
    installer.install(str(exe), desktop=False, paths=paths, make_shortcuts=False)
    assert installer._read_value(keys["mrpack_default"]) == "ModrinthApp.mrpack"  # Чужое умолчание не трогаем
    installer.uninstall(paths, schedule_exe_removal=False)
    assert installer._read_value(keys["mrpack_default"]) == "ModrinthApp.mrpack"
