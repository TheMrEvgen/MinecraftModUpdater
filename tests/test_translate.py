"""Перевод описаний: разметка, код и ссылки сохраняются, переводы запоминаются."""
import translate


class Response:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class Upper:
    """Подставной переводчик: переводит текст в верхний регистр (метки ⟦n⟧ не трогает)."""
    def __init__(self):
        self.sent = []

    def post(self, url, params=None, data=None, timeout=None):
        self.sent.append(data["q"])
        return Response([[[data["q"].upper(), data["q"]]], None, "en"])


def test_markdown_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(translate, "CACHE_FILE", str(tmp_path / "tr.json"))
    text = ("# Features\nSee [the wiki](https://example.com/wiki) and `config.json`.\n"
            "```\ncode stays\n```\n|---|---|\n![logo](https://img/x.png)")
    session = Upper()
    result = translate.translate_markdown(text, "ru", session)
    assert result.splitlines()[0] == "# FEATURES"
    assert "[THE WIKI](https://example.com/wiki)" in result and "`config.json`" in result
    assert "code stays" in result and "|---|---|" in result and "https://img/x.png" in result
    assert "code stays" not in session.sent[0] and "https://" not in session.sent[0]
    assert translate.translate_markdown(text, "ru", session) == result and len(session.sent) == 1  # Из кэша
    assert translate.translate_many(["", "hi"], "ru", session) == ["", "HI"]
