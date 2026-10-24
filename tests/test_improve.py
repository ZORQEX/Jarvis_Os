"""Self-improvement: the problem journal from JARVIS's own records, a change on a separate tested branch, applying it
only on a second confirmation (VERIFIED MOCK: a scripted Claude Code edits a temporary git repository)."""
import asyncio
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.improve as imp
from jarvis.tools.base import ToolContext, ToolInputError

from conftest import wait_done


def test_journal_finds_repeats_failures_and_slow_answers(core):
    d, db, t = core.director, core.db, time.time()
    cid = d.conversation_id()
    d.add_message(cid, "user", "Джарвис, открой папку колледж", meta={"source": "voice"})
    d.add_message(cid, "user", "Открой папку колледж!", meta={"source": "voice"})        # had to say it again
    d.add_message(cid, "user", "Который час?", meta={"source": "voice"})
    for _ in range(3):
        tid = core.tasks.create("найди контакт мама", cid, route="comms", level=1)
        db.execute("UPDATE tasks SET state='failed', error=? WHERE id=?", ("WhatsApp не ответил", tid))
    db.execute("INSERT INTO voice_turns(id, created_at, transcript, route, speech_end, first_audio_played) "
               "VALUES('v1', ?, 'расскажи анекдот', 'ai', ?, ?)", (t, t, t + 9.5))
    issues = {i["kind"]: i for i in imp.collect_issues(db)}
    assert issues["repeat"]["count"] == 1 and "колледж" in issues["repeat"]["evidence"]
    assert issues["failed"]["count"] == 3 and "WhatsApp не ответил" in issues["failed"]["evidence"]
    assert issues["slow"]["count"] == 1 and "9.5 с" in issues["slow"]["evidence"]
    assert list(issues)[0] == "failed"                             # the most frequent first



def test_a_problem_that_went_away_is_not_in_the_journal(core):
    """The same request done fine later = solved (four WhatsApp failures of 23.10 topped the journal after the fix)."""
    d, db, t = core.director, core.db, time.time()
    cid = d.conversation_id()

    def task(text, state, at, error=None):
        tid = core.tasks.create(text, cid, route="comms", level=1)
        db.execute("UPDATE tasks SET state=?, error=?, created_at=? WHERE id=?", (state, error, at, tid))

    task("Отправь маме в WhatsApp привет", "failed", t - 3600, "WhatsApp Web не дал найти контакт")
    task("Отправь Мама ❤️ в WhatsApp: привет", "failed", t - 3500, "WhatsApp Web не дал найти контакт")
    task("Отправь маме в WhatsApp привет.", "completed", t - 600)                 # works after the fix
    task("Открой папку с отчётами сейчас", "completed", t - 3000)
    task("Открой папку с отчётами сейчас", "failed", t - 60, "Папка не найдена")   # broke again after working
    issues = imp.collect_issues(db)
    assert [i["title"] for i in issues] == ["Ошибка: Папка не найдена"]


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "jarvis"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "app.py").write_text("SPEED = 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    monkeypatch.setattr(imp, "REPO", r)
    monkeypatch.setattr(imp, "WORK", tmp_path / "work")
    return r


class FakeClaudeCode:
    """Claude Code as the tests see it: edits the worktree it was given."""

    def __init__(self, edit=True):
        self.edit, self.workdirs = edit, []

    async def generate(self, req, _stream, progress):
        self.workdirs.append(req.workdir)
        if self.edit:
            Path(req.workdir, "app.py").write_text("SPEED = 2\n")
            Path(req.workdir, "test_app.py").write_text("def test_speed():\n    assert True\n")
        return SimpleNamespace(text="Причина: медленно. Исправлено.", model="test", usage={}, cost_usd=0,
                               total_ms=5, meta={})


class FakeManager:
    def __init__(self, cc):
        self.cc = cc

    def get(self, pid):
        return self.cc

    def record(self, *a, **k):
        pass


def ctx():
    return ToolContext(config=SimpleNamespace(claude_timeout_s=0), task_id="t_x")


async def test_change_happens_on_a_separate_tested_branch_and_applies_only_on_request(repo, monkeypatch):
    async def suite(wd, timeout=900):
        return {"pytest": {"code": 0, "tail": "2 passed"}}
    monkeypatch.setattr(imp, "run_suite", suite)
    cc = FakeClaudeCode()
    tool = imp.SelfImprove(FakeManager(cc), SimpleNamespace(claude_timeout_s=0))
    res = await tool.run({"goal": "Отвечать быстрее", "evidence": "9.5 с до звука"}, ctx())
    d = res.data
    assert d["branch"].startswith("improve/") and d["commits"] and d["tests_ok"]
    assert cc.workdirs[0] == d["worktree"] and Path(d["worktree"]).is_relative_to(imp.WORK)
    assert (repo / "app.py").read_text() == "SPEED = 1\n"           # the running copy is untouched
    assert (await tool.verify({}, res, ctx())).status == "verified"
    (repo / "other.py").write_text("x = 1\n")              # main moved on meanwhile: the merge is tested again
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "other")
    restarted = []
    apply = imp.SelfImproveApply(SimpleNamespace(), restart=lambda: restarted.append(1))
    args = apply.validate({"branch": d["branch"]}, ctx())
    out = await apply.run(args, ctx())
    assert (repo / "app.py").read_text() == "SPEED = 2\n" and restarted == [1]
    assert d["branch"] not in imp.pending_branches() and not Path(d["worktree"]).exists()
    assert (await apply.verify(args, out, ctx())).status == "verified"


async def test_failing_tests_are_never_applied(repo, monkeypatch):
    async def suite(wd, timeout=900):
        return {"pytest": {"code": 1, "tail": "1 failed"}}
    monkeypatch.setattr(imp, "run_suite", suite)
    tool = imp.SelfImprove(FakeManager(FakeClaudeCode()), SimpleNamespace(claude_timeout_s=0))
    res = await tool.run({"goal": "Сломать что-нибудь"}, ctx())
    assert not res.data["tests_ok"] and (await tool.verify({}, res, ctx())).status == "failed"
    apply = imp.SelfImproveApply(SimpleNamespace(), restart=lambda: None)
    with pytest.raises(RuntimeError, match="не прошли"):
        await apply.run(apply.validate({"branch": res.data["branch"]}, ctx()), ctx())
    assert (repo / "app.py").read_text() == "SPEED = 1\n"
    with pytest.raises(ToolInputError):
        apply.validate({"branch": "main"}, ctx())                 # only improvement branches


async def test_tools_need_the_persons_confirmation(core):
    spec = core.registry.get("self_improve").spec
    assert spec.risk.value == "high" and core.registry.get("self_improve_apply").spec.risk.value == "high"
    assert core.registry.get("improve_issues").spec.side_effects == "none"
    tid = await core.director.run_ui_tool("improve_issues", {})
    t = await wait_done(core, tid)
    assert t["state"] == "completed"
    await asyncio.sleep(0)


async def test_a_merge_that_breaks_the_tests_is_undone(repo, monkeypatch):
    calls = []

    async def suite(wd, timeout=900):
        calls.append(Path(wd))
        ok = Path(wd) != repo                       # the branch alone passes, the merged main does not
        return {"pytest": {"code": 0 if ok else 1, "tail": "ok" if ok else "1 failed"}}
    monkeypatch.setattr(imp, "run_suite", suite)
    res = await imp.SelfImprove(FakeManager(FakeClaudeCode()), SimpleNamespace(claude_timeout_s=0)).run(
        {"goal": "Быстрее"}, ctx())
    apply = imp.SelfImproveApply(SimpleNamespace(), restart=lambda: None)
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="После слияния"):
        await apply.run(apply.validate({"branch": res.data["branch"]}, ctx()), ctx())
    assert git(repo, "rev-parse", "HEAD") == head and (repo / "app.py").read_text() == "SPEED = 1\n"
    assert not git(repo, "status", "--porcelain", "--untracked-files=no").strip()
