"""Long-term memory: facts kept across conversations and given to the AI with every request."""
from jarvis import memory, router
from jarvis.agency.agency import jarvis_state_line

from conftest import wait_done


async def test_remember_recall_forget_by_voice(core):
    d = core.director
    t = await wait_done(core, await d.submit("Запомни, что я учусь в колледже, лабораторные лежат в ~/Projects/колледж", "voice"))
    assert t["state"] == "completed" and t["route"] == "tool"
    assert [f["text"] for f in memory.facts(core.db)] == ["Я учусь в колледже, лабораторные лежат в ~/Projects/колледж"]
    await wait_done(core, await d.submit("запомни: моя девушка в Telegram записана как Любимая"))
    t = await wait_done(core, await d.submit("Что ты обо мне помнишь?"))
    reply = d.messages(d.conversation_id())[-1]["text"]
    assert "колледже" in reply and "Любимая" in reply
    await wait_done(core, await d.submit("Забудь, что я учусь в колледже"))
    assert [f["text"] for f in memory.facts(core.db)] == ["Моя девушка в Telegram записана как Любимая"]


def test_the_ai_gets_the_facts_with_every_request(core):
    memory.add(core.db, "Пользователя зовут Тим")
    line = jarvis_state_line(core.director)
    assert "[Память о пользователе: Пользователя зовут Тим]" in line


def test_nearly_the_same_fact_replaces_the_old_one(core):
    memory.add(core.db, "Любимый цвет синий")
    f, new = memory.add(core.db, "любимый цвет — синий.")
    assert not new and len(memory.facts(core.db)) == 1


def test_notes_and_contacts_keep_their_commands():
    assert router.route("запиши заметку: купить хлеб").tool == "create_note"
    assert router.route("Запомни мой номер телефона +7 777").kind == "comms"
