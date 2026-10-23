"""Обновление самой программы: выбор выпуска, проверка контрольной суммы, замена .exe (без сети)."""
import hashlib
import os

import pytest

import self_update


def release(tag, prerelease=False, draft=False, assets=(self_update.EXE_ASSET, self_update.CHECKSUM_ASSET)):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "body": f"notes {tag}",
            "html_url": f"https://example.com/{tag}",
            "assets": [{"name": name, "browser_download_url": f"https://example.com/{tag}/{name}", "size": 10}
                       for name in assets]}


def test_pick_release_rules():
    releases = [release("v3.1.0"), release("v3.2.0-beta", prerelease=True), release("v3.3.0", draft=True),
                release("v3.0.0"), release("v9.0.0", assets=(self_update.EXE_ASSET,))]
    stable = self_update.pick_release(releases, "3.0.0", allow_prerelease=False)
    assert stable.version == "3.1.0" and stable.exe_url.endswith("/v3.1.0/ModUpdater.exe") and not stable.prerelease
    beta = self_update.pick_release(releases, "3.0.0-beta", allow_prerelease=True)
    assert beta.version == "3.2.0-beta" and beta.prerelease
    assert self_update.pick_release(releases, "3.2.0", allow_prerelease=True) is None  # Новее нет
    assert self_update.pick_release([release("v3.0.0")], "3.0.0-beta", False).version == "3.0.0"  # Релиз новее беты


def test_check_and_interval(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [release("v3.0.1"), release("v3.1.0-beta", prerelease=True)]

    monkeypatch.setattr(self_update.SESSION, "get", lambda *a, **k: Response())
    assert self_update.check("3.0.0", "owner/repo").version == "3.0.1"  # У стабильной версии — без бет
    assert self_update.check("3.0.0-beta", "owner/repo").version == "3.1.0-beta"
    assert self_update.check("3.0.0", "") is None
    assert self_update.needs_check(None) and self_update.needs_check(0)
    assert not self_update.needs_check(1000, now=1000 + 3600)
    assert self_update.needs_check(1000, now=1000 + self_update.CHECK_INTERVAL)


def test_parse_checksum():
    digest = "a" * 64
    assert self_update.parse_checksum(f"{digest}  ModUpdater.exe\r\n") == digest
    assert self_update.parse_checksum(digest.upper()) == digest
    for bad in ("", "abc  ModUpdater.exe", "z" * 64):
        with pytest.raises(ValueError):
            self_update.parse_checksum(bad)


class FakeSession:
    def __init__(self, payload: bytes, checksum: str):
        self.payload, self.checksum = payload, checksum

    def get(self, url, stream=False, **kwargs):
        session = self

        class Response:
            text = session.checksum
            headers = {"Content-Length": str(len(session.payload))}

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                for i in range(0, len(session.payload), 4):
                    yield session.payload[i:i + 4]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response()


def make_release():
    return self_update.pick_release([release("v3.1.0")], "3.0.0", False)


def test_download_verifies_checksum(tmp_path, monkeypatch):
    payload = b"new exe content"
    good = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(self_update, "SESSION", FakeSession(payload, f"{good}  ModUpdater.exe\n"))
    progress = []
    path = self_update.download(make_release(), str(tmp_path), lambda done, total: progress.append((done, total)))
    assert open(path, "rb").read() == payload and progress[-1] == (len(payload), len(payload))

    monkeypatch.setattr(self_update, "SESSION", FakeSession(payload, "b" * 64))
    with pytest.raises(ValueError):
        self_update.download(make_release(), str(tmp_path / "bad"))
    assert not os.listdir(tmp_path / "bad")  # Ни битого файла, ни .part

    monkeypatch.setattr(self_update, "SESSION", FakeSession(payload, good))
    with pytest.raises(self_update.UpdateCancelled):
        self_update.download(make_release(), str(tmp_path / "cancel"), cancelled=lambda: True)
    assert not os.listdir(tmp_path / "cancel")


def test_replace_executable_and_cleanup(tmp_path):
    current = tmp_path / "ModUpdater.exe"
    current.write_bytes(b"old")
    new = tmp_path / "download" / "ModUpdater-3.1.0.exe"
    new.parent.mkdir()
    new.write_bytes(b"new")
    old = self_update.replace_executable(str(new), str(current))
    assert current.read_bytes() == b"new" and open(old, "rb").read() == b"old" and not new.exists()
    (tmp_path / "ModUpdater.exe.123.old").write_bytes(b"older")
    (tmp_path / "Other.exe.old").write_bytes(b"not ours")
    assert self_update.cleanup_old(str(current)) == 2
    assert sorted(os.listdir(tmp_path)) == ["ModUpdater.exe", "Other.exe.old", "download"]


def test_replace_restores_on_failure(tmp_path):
    current = tmp_path / "ModUpdater.exe"
    current.write_bytes(b"old")
    with pytest.raises(OSError):
        self_update.replace_executable(str(tmp_path / "missing.exe"), str(current))
    assert current.read_bytes() == b"old"  # Не удалось — старая версия на месте
