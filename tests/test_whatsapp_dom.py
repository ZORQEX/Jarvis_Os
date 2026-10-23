"""WhatsApp Web layers that cover the search box (measured 2026-10-23: a «what's new» popover made every
«напиши маме» fail with «страница не отвечает»). Real Chromium on a local page that copies WhatsApp's structure."""
import shutil
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")
from jarvis.comms.whatsapp import SEL_SEARCH, WhatsAppProvider, chromium  # noqa: E402

pytestmark = pytest.mark.skipif(not chromium(), reason="needs Chromium")

PAGE = """<!doctype html><html><body style="margin:0;font:14px sans-serif">
<div id="side"><div contenteditable="true" role="textbox" aria-label="Поиск" style="margin:20px;width:300px;height:30px;border:1px solid #888"></div></div>
<div id="pane-side"><div role="listitem"><span title="Мама ❤️">Мама ❤️</span></div></div>
<div id="wa-popovers-bucket">%s</div></body></html>"""
POPOVER = """<div role="dialog" data-testid="popover" style="position:fixed;inset:0;background:rgba(0,0,0,.2)">
  <div>Новое в WhatsApp</div><div role="button" aria-label="Закрыть" style="width:20px;height:20px;background:#000"
  onclick="this.closest('[role=dialog]').remove()"></div></div>"""
STUCK = """<div style="position:fixed;inset:0;background:rgba(0,0,0,.1)"></div>"""


@pytest.fixture
async def page(tmp_path):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch(executable_path=chromium(), headless=True)
        pg = await b.new_page()
        yield pg
        await b.close()


def provider(tmp_path, pg):
    p = WhatsAppProvider.__new__(WhatsAppProvider)       # only the DOM helpers are exercised
    p.page = pg
    return p


async def box_of(pg):
    for s in SEL_SEARCH:
        el = await pg.query_selector(s)
        if el and await el.is_visible():
            return el
    raise AssertionError("search box not found")


async def test_whats_new_popover_is_closed_and_search_works(page, tmp_path):
    await page.set_content(PAGE % POPOVER)
    p = provider(tmp_path, page)
    await p._click(await box_of(page))
    assert await page.query_selector("#wa-popovers-bucket [role='dialog']") is None       # closed by its own button
    assert await page.evaluate("document.activeElement.getAttribute('aria-label')") == "Поиск"
    await page.keyboard.insert_text("Мама")
    assert await page.evaluate("document.activeElement.textContent") == "Мама"


async def test_unknown_layer_falls_back_to_the_element_itself(page, tmp_path):
    await page.set_content(PAGE % STUCK)
    p = provider(tmp_path, page)
    await p._click(await box_of(page))                     # no close button anywhere: focus the field directly
    assert await page.evaluate("document.activeElement.getAttribute('aria-label')") == "Поиск"
    assert "covered" in p.last_dialog
