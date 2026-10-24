"""Russian pages without touching the templates (app/i18n_ru.py, on with FASHION_LANG=ru inside JARVIS): whole UI
phrases are replaced, seasons and numbers carry over, data and scripts are never touched, and without the setting
the app stays exactly as upstream. Synthetic fixture only."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from test_factory_settlement import db, imported  # noqa: F401
from app.config import settings
from app.database import get_db
from app.i18n_ru import RU, translate, translate_html
from app.main import app


def test_phrases_seasons_and_numbers():
    assert translate("Paid 120.00 of 4 050.00 required") == "Оплачено 120.00 из 4 050.00 нужных"
    assert translate("No Ready batches in FW26") == "В FW26 нет партий готовых"
    assert translate("SS27: 1 brand table, 2 next actions.") == "SS27: таблиц брендов — 1, следующих шагов — 2."
    assert translate("Select Coat test") == "Выбрать: Coat test"
    assert translate("Coat test") is None and translate("Dior cruise") is None and translate("IDS") is None
    assert translate("Уже по-русски") is None


def test_markup_is_kept():
    page = ('<html lang="en"><label class="wide">Note<textarea placeholder="Choose a file">Ready</textarea></label>'
            '<a title="Settings">Ready &amp; shipping</a><script>const s = "Ready";</script><b>Dior cruise</b>')
    out = translate_html(page)
    assert '<label class="wide">Заметка<textarea placeholder="Выберите файл">Ready</textarea>' in out   # typed text kept
    assert '<a title="Настройки">Готово и отправки</a>' in out and 'const s = "Ready";' in out
    assert "<b>Dior cruise</b>" in out


def test_every_placeholder_is_carried_over():
    reworded = {"Paste a Claude API key and choose Sonnet {0} or Haiku {1} for reading orders, drafting tables and AI Help."}
    wrong = [key for key, value in RU.items() if key not in reworded
             and sorted(re.findall(r"\{\d\}|\{S\}", key)) != sorted(re.findall(r"\{\d\}|\{S\}", value))]
    assert not wrong, wrong


@pytest.fixture
def client(db):  # noqa: F811
    app.dependency_overrides[get_db] = lambda: (yield db)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def test_pages_in_russian_only_when_asked(client, db, tmp_path, monkeypatch):  # noqa: F811
    imported(db, tmp_path)
    english = client.get("/board").text
    assert "Search everything" in english                       # upstream behaviour without the setting
    monkeypatch.setattr(settings, "lang", "ru")
    for path in ("/board", "/", "/money", "/settings", "/factories", "/pricing"):
        page = client.get(path).text
        assert '<html lang="ru"' in page and "Поиск по всему" in page, path
        visible = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", page, flags=re.S)
        for phrase in ("Search everything", ">Add<", "Audit history", "Money is calculated from the rules"):
            assert phrase not in visible, (path, phrase)
