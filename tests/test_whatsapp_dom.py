"""WhatsApp Web layers that cover the search box (measured 2026-10-23: a «what's new» popover made every
«напиши маме» fail with «страница не отвечает»). Real Chromium on a local page that copies WhatsApp's structure."""

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


# Search results that arrive late and in parts, like WhatsApp Web's own search.
CHATS = """<!doctype html><html><body>
<div id="side"><div contenteditable="true" role="textbox" aria-label="Поиск" style="width:300px;height:30px"></div></div>
<div id="pane-side"></div>
<div id="main"><header><span dir="auto" title="%s">%s</span></header><footer><div contenteditable="true"
  style="width:300px;height:30px"></div></footer></div>
<script>
const all = ["Рома", "Мама ❤️", "Работа", "Рома Петров", "Дима"];
const pane = document.getElementById('pane-side');
const show = (names) => { pane.innerHTML = names.map((n) => `<div role="listitem"><span title="${n}">${n}</span></div>`).join(''); };
show(all);
document.querySelector('#side [contenteditable]').addEventListener('input', (e) => {
  const q = e.target.textContent.toLowerCase();
  const hits = all.filter((n) => n.toLowerCase().includes(q));
  setTimeout(() => show(hits.slice(0, 1)), %d);            // first result
  setTimeout(() => show(hits), %d);                        // the rest a moment later
});
</script></body></html>"""


async def test_search_waits_for_all_results_not_a_fixed_pause(page, tmp_path):
    import time
    await page.set_content(CHATS % ("", "", 300, 700))
    p = provider(tmp_path, page)
    t0 = time.time()
    await p._type_search("рома")
    took = time.time() - t0
    assert await p._chat_titles() == ["Рома", "Рома Петров"]       # the second «Рома» is not missed
    assert took < 1.6, took


async def test_slow_results_are_still_waited_for(page, tmp_path):
    await page.set_content(CHATS % ("", "", 1200, 1500))
    p = provider(tmp_path, page)
    await p._type_search("рома")
    assert await p._chat_titles() == ["Рома", "Рома Петров"]


async def test_opening_a_chat_stops_waiting_when_it_is_listed(page, tmp_path):
    import time
    await page.set_content(CHATS % ("", "", 200, 2000))
    p = provider(tmp_path, page)
    t0 = time.time()
    await p._type_search("Мама ❤️", until="Мама ❤️")
    assert "Мама ❤️" in await p._chat_titles()
    assert time.time() - t0 < 1.0


async def test_the_chat_already_open_is_not_searched_again(page, tmp_path):
    from jarvis.comms.base import Contact
    await page.set_content(CHATS % ("Мама ❤️", "Мама ❤️", 100, 200))
    p = provider(tmp_path, page)
    await p._open_chat(Contact("whatsapp", "chat:Мама ❤️", "Мама ❤️", "", "WhatsApp"))
    assert await page.evaluate("document.querySelector('#side [contenteditable]').textContent") == ""


async def test_another_open_chat_is_never_taken_for_the_one_asked(page, tmp_path):
    from jarvis.comms.base import Contact
    await page.set_content(CHATS % ("Мама", "Мама", 100, 200))       # «Мама», not «Мама ❤️»
    p = provider(tmp_path, page)
    from jarvis.comms.base import CommsError
    with pytest.raises(CommsError, match="не тот чат"):     # searched, clicked; the header says «Мама» → refused
        await p._open_chat(Contact("whatsapp", "chat:Мама ❤️", "Мама ❤️", "", "WhatsApp"))
    assert await page.evaluate("document.querySelector('#side [contenteditable]').textContent") == "Мама ❤️"
