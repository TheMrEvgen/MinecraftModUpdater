"""Сборка для сервера: без модов «только для клиента», с настройками, сервером Fabric или установщиком Forge/NeoForge."""
import zipfile

import server_pack


class Response:
    def __init__(self, data=None, content=b""):
        self._data, self.content = data, content

    def json(self):
        return self._data

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.content


class Session:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout=None, stream=False):
        self.urls.append(url)
        if url.endswith("/versions/installer"):
            return Response([{"version": "1.1.2", "stable": True}])
        return Response(content=b"JAR")


class Offline:
    def get(self, *a, **k):
        raise OSError("нет сети")


def make_game(tmp_path):
    game = tmp_path / "game"
    (game / "mods").mkdir(parents=True)
    (game / "config").mkdir()
    (game / "config" / "lithium.properties").write_text("x=1")
    paths = {}
    for name in ("lithium.jar", "sodium.jar", "unknown.jar", "off.jar.disabled"):
        paths[name] = game / "mods" / name
        paths[name].write_bytes(b"PK")
    entries = [
        {"name": "Lithium", "filepath": str(paths["lithium.jar"]), "client_side": "optional", "server_side": "optional"},
        {"name": "Sodium", "filepath": str(paths["sodium.jar"]), "client_side": "required", "server_side": "unsupported"},
        {"name": "Unknown", "filepath": str(paths["unknown.jar"])},
        {"name": "Off", "filepath": str(paths["off.jar.disabled"]), "disabled": True},
    ]
    return game, entries


def test_split_and_fabric_server(tmp_path):
    game, entries = make_game(tmp_path)
    server, client = server_pack.split_entries(entries + entries[:1])  # Повтор не дублируется
    assert [e["name"] for e in server] == ["Lithium", "Unknown"] and [e["name"] for e in client] == ["Sodium"]
    target = str(tmp_path / "server.zip")
    session = Session()
    result = server_pack.build(target, [e["filepath"] for e in server], str(game), "1.21.1", "fabric", "0.16.9",
                               6144, ["Sodium"], session)
    names = set(zipfile.ZipFile(target).namelist())
    assert {"mods/lithium.jar", "mods/unknown.jar", "config/lithium.properties", "fabric-server-launch.jar",
            "start.bat", "start.sh", "README_SERVER.txt"} <= names
    assert "mods/sodium.jar" not in names and result.launcher == "start.bat" and not result.warnings
    assert session.urls[-1].endswith("/loader/1.21.1/0.16.9/1.1.2/server/jar")
    archive = zipfile.ZipFile(target)
    assert "-Xmx6G" in archive.read("start.bat").decode() and "Sodium" in archive.read("README_SERVER.txt").decode()
    assert "eula=true" not in archive.read("start.sh").decode()  # EULA принимает владелец сервера, не программа


def test_neoforge_installer_and_offline(tmp_path):
    game, entries = make_game(tmp_path)
    session = Session()
    result = server_pack.build(str(tmp_path / "neo.zip"), [entries[0]["filepath"]], str(game), "1.21.1", "neoforge",
                               "21.1.77", 4096, session=session)
    names = set(zipfile.ZipFile(result.path).namelist())
    assert {"neoforge-installer.jar", "install.bat", "install.sh", "user_jvm_args.txt"} <= names
    assert session.urls == ["https://maven.neoforged.net/releases/net/neoforged/neoforge/21.1.77/neoforge-21.1.77-installer.jar"]

    offline = server_pack.build(str(tmp_path / "off.zip"), [entries[0]["filepath"]], str(game), "1.20.1", "forge",
                                "47.3.0", session=Offline())
    names = set(zipfile.ZipFile(offline.path).namelist())
    assert offline.launcher == "README" and offline.warnings and "mods/lithium.jar" in names
    assert not any(n.endswith("installer.jar") for n in names)
