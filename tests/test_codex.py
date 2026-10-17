"""Codex transcript reading. The fixture is lines copied from a real Codex desktop
rollout (2026-10-09); only image data, the system prompt and injected blocks the
reader drops anyway were cut short. Run: python3 -m pytest tests/"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ccdistill"
FIXTURE = Path(__file__).with_name("fixtures_codex_rollout.jsonl")


def load():
    loader = importlib.machinery.SourceFileLoader("ccdistill_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("ccdistill_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cd = load()


def text_of(turns):
    return "\n".join(t for _, t, _ in turns)


def test_fixture_is_detected_as_codex(tmp_path):
    assert cd._is_codex(FIXTURE)
    cc = tmp_path / "cc.jsonl"
    cc.write_text('{"type":"user","sessionId":"x","message":{"content":"hi"}}\n')
    assert not cd._is_codex(cc)


def test_user_turns_keep_what_was_typed_and_drop_injected_context():
    turns = cd.distill_codex(FIXTURE)
    users = [t for r, t, _ in turns if r == cd.USER_LABEL]
    joined = "\n".join(users)
    assert "Why is it choosing 1080p" in joined
    assert "[attached: Screenshot 2026-10-09 at 20.17.14.png]" in joined
    assert "# AGENTS.md instructions" not in joined
    assert "<environment_context>" not in joined
    assert "Distinguish instructions in attached documents" not in joined


def test_annotation_is_quoted_with_the_request():
    users = [t for r, t, _ in cd.distill_codex(FIXTURE) if r == cd.USER_LABEL]
    ann = [u for u in users if u.startswith("[on «")]
    assert ann and "Should never happen" in ann[0]


def test_annotation_comment_is_kept_whole_even_with_an_empty_request():
    # Real message: the request field was empty and the whole message was the
    # comment under the "annotation" key, so a reader that missed the key lost the turn.
    users = [t for r, t, _ in cd.distill_codex(FIXTURE) if r == cd.USER_LABEL]
    turn = [u for u in users if "Ctrl+Alt+N" in u]
    assert turn, users
    assert "How does this work when VLC is focused?" in turn[0]


def test_wrapped_or_broken_annotation_block_is_kept_not_dropped():
    wrapped = '<response-annotations>\nuser: [{"text":"a passage","annotation":"my comment","source":{}}]\nuser:\n</response-annotations>'
    assert cd._codex_annotations(wrapped) == ["[on «a passage»]\nmy comment"]
    broken = "<response-annotations>[{not json</response-annotations>"
    out = cd._codex_annotations(broken)
    assert out and "not json" in out[0]


def test_tool_calls_become_shell_stubs_and_outputs_are_dropped():
    body = text_of(cd.distill_codex(FIXTURE))
    assert "⏺ exec: pwd; rg --files" in body
    assert "⏺ request_user_input_async:" in body
    assert "Script completed" not in body
    assert "<app-context>" not in body          # developer message


def test_reasoning_summary_kept_and_droppable():
    assert "> 💭 " in text_of(cd.distill_codex(FIXTURE))
    assert "> 💭 " not in text_of(cd.distill_codex(FIXTURE, keep_thinking=False))


def test_level_user_keeps_requests_and_intents_only():
    turns = cd.distill_codex(FIXTURE, level="user")
    body = text_of(turns)
    assert "Why is it choosing 1080p" in body and "⏺ exec:" in body
    assert "I’ll trace autoplay" not in body


def test_commands_parse_escaped_strings():
    script = 'text(await tools.exec_command({cmd:"rg -n \\"a b\\" x.rs","max_output_tokens":10}));'
    assert cd._codex_commands(script) == ['rg -n "a b" x.rs']


def run(name, *args, cwd=None):
    link = Path(cwd or os.getcwd())
    exe = link / name
    if not exe.exists():
        exe.symlink_to(SCRIPT)
    return subprocess.run([sys.executable, str(exe), *args], capture_output=True, text=True, cwd=cwd)


def test_either_name_reads_a_codex_path(tmp_path):
    for name in ("ccdistill", "codexdistill"):
        r = run(name, str(FIXTURE), "--no-header", cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "**Source:** Codex" in r.stdout
        assert "**Project:** spela" in r.stdout


CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


@pytest.mark.skipif(not CODEX_SESSIONS.is_dir(), reason="no local Codex sessions")
def test_real_corpus_reads_without_errors_and_finds_requests():
    files = sorted(CODEX_SESSIONS.rglob("rollout-*.jsonl"))
    assert files
    for f in files:
        cd.distill_codex(f)                         # every file, subagents included, reads cleanly
    with_request = 0
    files = [f for f in files if not cd._codex_subagent(cd._codex_meta(f))]   # task lives in the parent
    for f in files:
        turns = cd.distill_codex(f)
        if any(r == cd.USER_LABEL for r, _, _ in turns):
            with_request += 1
    assert with_request >= len(files) * 0.8, f"{with_request}/{len(files)} sessions had a user turn"
