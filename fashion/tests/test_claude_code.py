"""Claude through the local Claude Code CLI (the owner's subscription): the same typed tools reach the app over the
MCP bridge and a private socket, a change stays a proposal, and a broken CLI falls back to the built-in commands.
VERIFIED MOCK: a scripted stand-in for the `claude` command speaks real MCP to the real bridge."""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap

import pytest
from sqlalchemy import select

from test_factory_settlement import db, imported  # noqa: F401
from app import agent, claude_code
from app.config import settings
from app.models import AgentProposal, CustomerPricingRule

FAKE_CLI = textwrap.dedent('''\
    #!{python}
    """Stand-in for Claude Code: starts the MCP server from --mcp-config and calls the tools of SCRIPT in order."""
    import json, os, subprocess, sys
    argv = sys.argv[1:]
    log = {{"argv": argv, "prompt": sys.stdin.read()}}
    server = json.load(open(argv[argv.index("--mcp-config") + 1]))["mcpServers"]["fashion"]
    mcp = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def rpc(i, method, params=None):
        mcp.stdin.write(json.dumps({{"jsonrpc": "2.0", "id": i, "method": method, "params": params or {{}}}}) + "\\n")
        mcp.stdin.flush()
        return json.loads(mcp.stdout.readline())["result"]
    rpc(1, "initialize", {{"protocolVersion": "2025-06-18"}})
    log["tools"] = [t["name"] for t in rpc(2, "tools/list")["tools"]]
    outputs = []
    for n, (name, args) in enumerate(json.loads(os.environ["SCRIPT"])):
        out = rpc(10 + n, "tools/call", {{"name": name, "arguments": args}})
        outputs.append(json.loads(out["content"][0]["text"]))
    log["outputs"] = outputs
    json.dump(log, open(os.environ["LOG"], "w"), ensure_ascii=False)
    print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": os.environ["REPLY"]}}]}}}}))
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": os.environ["REPLY"]}}))
''')


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLI.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FASHION_CLAUDE_CLI", str(path))
    monkeypatch.setenv("LOG", str(tmp_path / "cli.json"))
    monkeypatch.setattr(settings, "ai_provider", "claude-code")
    monkeypatch.setattr(settings, "ai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")

    def run(script, reply="Готово."):
        monkeypatch.setenv("SCRIPT", json.dumps(script))
        monkeypatch.setenv("REPLY", reply)
    run.log = lambda: json.loads((tmp_path / "cli.json").read_text())
    return run


def test_tools_reach_the_app_and_a_change_stays_a_proposal(db, tmp_path, fake_cli):  # noqa: F811
    imported(db, tmp_path)
    assert settings.claude_code
    fake_cli([["calculate_factory_settlement", {"season": "SS27"}],
              ["propose_changes", {"summary": "IDS 35 %", "actions": [
                  {"action": "set_customer_markup", "params": {"customer": "IDS", "markup": "35"}}]}]],
             reply="Подготовила: наценка IDS 35 %. Подтвердите.")
    result = agent.handle(db, "a" * 16, "Сколько должна фабрике и поставь IDS тридцать пять процентов")
    log = fake_cli.log()
    assert "propose_changes" in log["tools"] and "select_items" in log["tools"]
    assert "--tools" in log["argv"] and log["argv"][log["argv"].index("--tools") + 1] == ""      # no shell or files
    assert "Новое сообщение владелицы" in log["prompt"] and "IDS" in log["prompt"]
    assert log["outputs"][0]["factory_total"]                  # the real settlement, computed by the app
    assert result.reply.startswith("Подготовила") and [c["name"] for c in result.tool_calls] == \
        ["calculate_factory_settlement", "propose_changes"]
    assert result.proposal and result.proposal["status"] == "pending"
    assert db.scalar(select(AgentProposal)).status == "pending"
    assert not db.scalars(select(CustomerPricingRule)).all()     # nothing saved before «Подтвердить»


def test_a_question_ends_the_turn_with_options(db, tmp_path, fake_cli):  # noqa: F811
    imported(db, tmp_path)
    fake_cli([["ask_user", {"question": "Для какого клиента?", "options": ["AA", "IDS"]}]], reply="")
    result = agent.handle(db, "b" * 16, "Сделай документы")
    assert result.question == {"question": "Для какого клиента?", "options": ["AA", "IDS"]}
    assert result.reply == "Для какого клиента?"


def test_a_failing_cli_answers_by_the_built_in_commands(db, tmp_path, monkeypatch, fake_cli):  # noqa: F811
    imported(db, tmp_path)
    broken = tmp_path / "broken"
    broken.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({{'type': 'result', 'is_error': True, "
                      "'result': 'Not logged in · Please run /login'}))\n")
    broken.chmod(0o755)
    monkeypatch.setenv("FASHION_CLAUDE_CLI", str(broken))
    result = agent.handle(db, "c" * 16, "Расскажи что-нибудь интересное про сезон")
    assert result.reply.startswith("⚠ Claude Code не вошёл в аккаунт")


def test_no_cli_means_no_claude_code(monkeypatch, tmp_path):
    monkeypatch.setenv("FASHION_CLAUDE_CLI", str(tmp_path / "missing"))
    monkeypatch.setattr(settings, "ai_provider", "claude-code")
    assert not claude_code.available() and not settings.claude_code


def test_child_environment_has_no_keys(monkeypatch):
    monkeypatch.setenv("FASHION_AI_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = claude_code._env()
    assert "FASHION_AI_API_KEY" not in env and "CLAUDECODE" not in env and env.get("PATH") == os.environ["PATH"]
