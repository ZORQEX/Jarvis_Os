"""Self-development: skills JARVIS builds for itself, and no artificial limits on thinking time."""
import asyncio
import json
import stat

import pytest

from jarvis.config import limit
from jarvis.providers.claude_code import build_argv
from jarvis.skills import list_skills, read_manifest, skills_root
from jarvis.tools.base import ToolResult

from conftest import wait_for


def running(core, text="x"):
    from jarvis.tasks import S
    t = core.tasks.create(text, None)
    core.tasks.transition(t, S.RUNNING)
    return t


def make_skill(root, name="echo_upper", body='echo "$1" | tr a-z A-Z', **extra):
    d = root / name
    d.mkdir(parents=True)
    run = d / "run.sh"
    run.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n")
    run.chmod(run.stat().st_mode | stat.S_IXUSR)
    m = {"name": name, "title": "Заглавные буквы", "description": "Делает текст заглавным", "run": ["./run.sh"],
         "args": [{"name": "text", "description": "текст"}], "example": ["привет"], **extra}
    (d / "skill.json").write_text(json.dumps(m, ensure_ascii=False))
    return d


async def execute(core, tool, args):
    t = running(core)
    return await core.director.gw.execute(t, tool, args, "test")


def test_manifest_must_be_complete_and_stay_inside(core):
    root = skills_root(core.config)
    make_skill(root)
    bad = make_skill(root, "escape", run=["../../bin/sh"])
    (bad / "skill.json").write_text(json.dumps({"name": "escape", "run": ["./missing.sh"]}))
    other = root / "wrongname"
    other.mkdir()
    (other / "skill.json").write_text(json.dumps({"name": "different", "run": ["./run.sh"]}))
    assert [s["name"] for s in list_skills(core.config)] == ["echo_upper"]
    assert read_manifest(bad) is None and read_manifest(other) is None


async def test_skill_list_and_run_through_the_gateway(core):
    make_skill(skills_root(core.config))
    out = await execute(core, "skill_list", {})
    assert out.status == "ok" and out.result.data["skills"][0]["name"] == "echo_upper"
    core.policy.full_access = True                       # moderate risk: no question in full-access mode
    out = await execute(core, "skill_run", {"name": "echo_upper", "args": ["hello"]})
    assert out.status == "ok" and out.result.data["output"].strip() == "HELLO"
    assert out.verification.status == "unverified"       # exit code 0 is not proof of the result
    out = await execute(core, "skill_run", {"name": "nope", "args": []})
    assert out.status == "invalid"


async def test_failing_skill_is_reported(core):
    make_skill(skills_root(core.config), "broken", body='echo "нет файла" >&2; exit 3')
    core.policy.full_access = True
    out = await execute(core, "skill_run", {"name": "broken"})
    assert out.status == "failed" and "код 3" in out.error and "нет файла" in out.error


async def test_skill_create_is_confirmed_and_verified_by_its_manifest(core, monkeypatch):
    seen = {}

    async def fake_claude(manager, ctx, args, mode):
        seen.update(args=args, perms=ctx.payload["req"].permissions)
        make_skill(skills_root(core.config).parent / "tmp_build")      # unrelated folder: must not count
        d = skills_root(core.config) / "video_download"
        (d / "run.sh").write_text("#!/usr/bin/env bash\necho ok\n")
        (d / "run.sh").chmod(0o755)
        (d / "skill.json").write_text(json.dumps({"name": "video_download", "title": "Скачать видео",
                                                  "description": "yt-dlp", "run": ["./run.sh"], "args": [{"name": "url"}]}))
        return ToolResult({"text": "готово"}, "Claude Code: готово", card="agent_result")
    import jarvis.agency.tools as at
    monkeypatch.setattr(at, "_run_claude", fake_claude)
    t = running(core, "научись скачивать видео")
    job = asyncio.ensure_future(core.director.gw.execute(t, "skill_create",
                                                         {"name": "video_download", "instruction": "скачивать видео по ссылке"}, "test"))
    await wait_for(lambda: core.approvals.pending())       # high risk: the person confirms first
    a = core.approvals.pending()[0]
    assert a["tool_id"] == "skill_create"
    core.director.decide(a["id"], True)
    out = await job
    assert out.status == "ok" and out.verification.status == "verified"
    assert seen["args"]["workdir"].endswith("/video_download") and set(seen["perms"]) == {"browser", "download", "install"}
    assert "GitHub" in seen["args"]["instruction"] and "skill.json" in seen["args"]["instruction"]


async def test_skill_create_without_manifest_fails_honestly(core, monkeypatch):
    async def fake_claude(manager, ctx, args, mode):
        return ToolResult({"text": "не вышло"}, "Claude Code: готово", card="agent_result")
    import jarvis.agency.tools as at
    monkeypatch.setattr(at, "_run_claude", fake_claude)
    core.policy.full_access = True
    t = running(core)
    job = asyncio.ensure_future(core.director.gw.execute(t, "skill_create", {"name": "ocr_text", "instruction": "распознавать текст"}, "t"))
    await wait_for(lambda: core.approvals.pending())
    core.director.decide(core.approvals.pending()[0]["id"], True)
    out = await job
    assert out.status == "failed" and "skill.json" in out.error


def test_skill_names_are_safe(core):
    from jarvis.skills import SkillCreate
    tool = core.director.gw.registry.get("skill_create")
    assert isinstance(tool, SkillCreate)
    for bad in ("../x", "a", "rm -rf", "1abc"):
        with pytest.raises(Exception):
            tool.validate({"name": bad, "instruction": "x"}, None)


# ---------------------------------------------------------------- no artificial limits
def test_no_time_budget_or_turn_limits_by_default(core):
    c = type(core.config)()
    assert c.claude_timeout_s == 0 and c.claude_budget_usd == 0 and c.claude_max_turns == 0 and c.max_tool_rounds == 0
    assert limit(0) is None and limit(None) is None and limit(30) == 30.0
    argv = build_argv("x", "change")
    assert "--max-budget-usd" not in argv and "--max-turns" not in argv
    argv = build_argv("x", "change", budget=3, max_turns=50)
    assert argv[argv.index("--max-budget-usd") + 1] == "3.00" and argv[argv.index("--max-turns") + 1] == "50"
    for tid in ("computer_task", "skill_create", "agent_generate", "skill_run"):
        assert core.director.gw.registry.get(tid).spec.timeout_s is None, tid


def test_computer_task_may_always_install_and_download(core, home):
    core.config.code_workspaces = [str(home / "Projects")]
    tool = core.director.gw.registry.get("computer_task")
    a = tool.validate({"instruction": "сделай конвертер видео", "new_folder": "conv"}, None)
    assert {"install", "download"} <= set(a["permissions"]) and "browser" not in a["permissions"]


def test_state_line_lists_skills(core):
    from jarvis.agency.agency import jarvis_state_line
    make_skill(skills_root(core.config))
    assert "навыки: echo_upper" in jarvis_state_line(core.director)
