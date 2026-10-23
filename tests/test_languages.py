"""Several languages: which voice reads which sentence, when the second recogniser re-hears a phrase, JARVIS's own
phrases in the language the person speaks."""
import asyncio

from jarvis.voice.lang import detect, runs
from jarvis.voice.lexicon import Lexicon, prefer_second
from jarvis.voice.speech import SpeechManager

from conftest import ScriptedClaude, install, wait_done, wait_for


def test_detect_language_of_a_sentence():
    assert detect("Done. I've sent the message to your mom.") == "en"
    assert detect("Hola, ¿cómo estás?") == "es"
    assert detect("Guten Tag, wie geht es dir?") == "de"
    assert detect("Привіт, як справи?") == "uk"
    assert detect("Привет, как дела?") == "ru"
    assert detect("Зараз дванадцята година", "uk") == "uk"     # no Ukrainian-only letter: the conversation decides


def test_each_sentence_gets_the_voice_of_its_language():
    assert runs("Готово. Открыл YouTube Music.", "ru") == [("ru", "Готово. Открыл YouTube Music.")]
    assert runs("The capital of France is Paris. Столица Франции — Париж.", "en") == \
        [("en", "The capital of France is Paris."), ("ru", "Столица Франции — Париж.")]
    assert runs("Переведено: Hello, how are you doing today?", "ru") == \
        [("ru", "Переведено:"), ("en", "Hello, how are you doing today?")]
    assert runs("WhatsApp.", "ru") == [("ru", "WhatsApp.")]


def test_second_recogniser_only_for_phrases_that_are_not_russian(tmp_path):
    words = tmp_path / "ru.txt"
    words.write_text("\n".join(f"{w} 100" for w in "напиши маме привет открой какая погода завтра вот там ола".split()))
    lex = Lexicon(str(words))
    lex.add(["ютуб"])
    assert lex.russian("Напиши маме в WhatsApp привет.")          # a brand inside a Russian phrase
    assert lex.russian("Открой Ютуб.")
    assert lex.russian("")                                          # silence: nothing to re-hear
    assert not lex.russian("The Weather in London Today Is Twelf To Grees and Cloudy.")   # GigaAM on English
    assert not lex.russian("Хавуарью.")                             # «How are you» spelled in Cyrillic
    assert not lex.russian("Ола Кумостас, oyatesol")                # Spanish
    assert prefer_second("How are you?") and prefer_second("Привіт!")
    assert not prefer_second("Джарвиз, Тернонзе Мюзик.")            # Parakeet heard Russian too: keep GigaAM


class Engine:
    """Records (language, text) per spoken piece."""
    multilingual = True

    def __init__(self):
        self.calls = []

    async def synth(self, text, lang="ru"):
        self.calls.append((lang, text))
        return b"RIFF" + bytes(100)


async def test_english_question_gets_english_hold_phrase_and_voice(core):
    eng = Engine()
    sp = SpeechManager(core.db, core.bus, eng)
    core.director.speech = sp
    g = ScriptedClaude(core.config, [["It is sunny today."]])
    orig = g.run_tool_loop

    async def slow(*a, **k):
        await asyncio.sleep(3.8)
        return await orig(*a, **k)
    g.run_tool_loop = slow
    install(core, claude=g)
    tid = await core.director.submit("Jarvis, what's the weather like today?", "voice")
    await wait_done(core, tid, timeout=8)
    await wait_for(lambda: len(eng.calls) >= 2)
    assert eng.calls[0][0] == "en" and eng.calls[0][1] in ("One moment.", "On it.")
    assert eng.calls[1] == ("en", "It is sunny today.")
    assert sp.lang == "en"


def test_bare_wake_word_is_answered_in_its_language():
    from jarvis.router import route
    assert route("Jarvis.").reply == "Yes?" and route("Джарвис.").reply == "Да?"
