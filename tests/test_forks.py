"""Forked sessions: each read on its own, and a whole family read without repeats.

The shape is the one Claude Code writes when a session is forked (checked on three real
desktop forks, 2026-11-22): the fork's file opens with the parent's first records copied
line for line (same uuids, the PARENT's sessionId), then carries on with records under its
own id, the first few of them metadata with no timestamp (custom-title, relocated,
agent-name). The parent's file goes on after the fork point under the parent's id.
Run: python3 -m pytest tests/"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import importlib.machinery
import importlib.util

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ccdistill"


def load():
    loader = importlib.machinery.SourceFileLoader("ccdistill_forks", str(SCRIPT))
    spec = importlib.util.spec_from_loader("ccdistill_forks", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cd = load()

P = "aaaaaaaa-1111-4111-8111-111111111111"     # the parent
F1 = "bbbbbbbb-2222-4222-8222-222222222222"    # forked from the parent
F2 = "cccccccc-3333-4333-8333-333333333333"    # forked from the parent at the same point
F3 = "dddddddd-4444-4444-8444-444444444444"    # forked from F1
BASE = datetime(2026, 11, 21, 1, 0, 0, tzinfo=timezone.utc)


def ts(minute):
    return (BASE + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jl(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def turn(sid, minute, who, text):
    kind = "user" if who == "u" else "assistant"
    content = text if who == "u" else [{"type": "text", "text": text}]
    return jl({"parentUuid": None, "isSidechain": False, "type": kind,
               "message": {"role": kind, "content": content}, "uuid": f"{sid[:4]}-{minute}-{who}",
               "timestamp": ts(minute), "sessionId": sid, "version": "2.1.300", "gitBranch": "master",
               "cwd": "/work/proj"})


def head_meta(sid):
    return [jl({"type": "bridge-session", "sessionId": sid, "bridgeSessionId": "x", "lastSequenceNum": 0}),
            jl({"type": "relocated", "relocatedCwd": "/work/proj", "sessionId": sid})]


def fork_meta(sid, title):
    return [jl({"type": "custom-title", "customTitle": title, "sessionId": sid}),
            jl({"type": "relocated", "relocatedCwd": "/work/proj", "sessionId": sid}),
            jl({"type": "agent-name", "agentName": title, "sessionId": sid})]


def conversation(sid, start, n, label):
    lines = []
    for k in range(n):
        lines.append(turn(sid, start + 2 * k, "u", f"{label} question {k}"))
        lines.append(turn(sid, start + 2 * k + 1, "a", f"{label} answer {k}"))
    return lines


def write(path, lines):
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


def family(tmp_path, with_parent=True, nested=False):
    """A project folder under a throwaway HOME holding the parent and its forks."""
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-work-proj"
    proj.mkdir(parents=True)
    trunk = head_meta(P) + conversation(P, 0, 5, "trunk")          # minutes 0-9: shared
    after = conversation(P, 30, 3, "trunk later")                   # the parent goes on
    if with_parent:
        write(proj / f"{P}.jsonl", trunk + after)
    f1 = trunk + fork_meta(F1, "Branch one") + conversation(F1, 12, 4, "branch one")
    write(proj / f"{F1}.jsonl", f1)
    write(proj / f"{F2}.jsonl", trunk + fork_meta(F2, "Branch two") + conversation(F2, 12, 2, "branch two"))
    if nested:
        f1_at_fork = f1[: len(trunk) + 3 + 4]                       # its metadata and two of its turns
        write(proj / f"{F3}.jsonl", f1_at_fork + fork_meta(F3, "Branch three") + conversation(F3, 40, 2, "branch three"))
    return home, proj


def run_cli(home, *args):
    env = dict(os.environ, HOME=str(home))
    env.pop("CCDISTILL_ARCHIVE_ROOTS", None)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


# --- which session a file is ----------------------------------------------------------------

def test_a_fork_file_is_its_own_session_not_its_parents(tmp_path):
    _, proj = family(tmp_path)
    assert cd._own_id(str(proj / f"{F1}.jsonl")) == F1
    assert cd._own_id(str(proj / f"{P}.jsonl")) == P


def test_the_parents_chain_holds_only_the_parents_file(tmp_path):
    _, proj = family(tmp_path)
    assert [Path(c).name for c in cd._chain_for_session(P, [proj])] == [f"{P}.jsonl"]
    assert [Path(c).name for c in cd._chain_for_session(F1, [proj])] == [f"{F1}.jsonl"]


def test_fork_info_names_the_parent_and_the_fork_point(tmp_path):
    _, proj = family(tmp_path, nested=True)
    fi = cd._fork_info(str(proj / f"{F1}.jsonl"))
    assert fi["parent"] == P and fi["own"] == F1
    assert fi["fork_ts"] == ts(9)[:19]                 # the last copied record
    assert fi["first_own_ts"] == ts(12)[:19]           # its first own record with a time
    assert cd._fork_info(str(proj / f"{P}.jsonl")) is None
    assert cd._fork_info(str(proj / f"{F3}.jsonl"))["parent"] == F1   # the nearest parent, not the root


# --- reading one session --------------------------------------------------------------------

def test_reading_a_fork_shows_only_its_own_turns_and_names_its_parent(tmp_path):
    home, _ = family(tmp_path)
    r = run_cli(home, F1, "--no-header")
    assert r.returncode == 0, r.stderr
    body = r.stdout.split("\n### ", 1)[1]
    assert "branch one question 0" in body and "branch one answer 3" in body
    assert "trunk question" not in body and "branch two" not in body
    assert P in r.stdout and "Forked from" in r.stdout
    assert "--inherited" in r.stdout                   # how to get the parent's turns here


def test_inherited_puts_the_shared_turns_back_with_a_marker(tmp_path):
    home, _ = family(tmp_path)
    r = run_cli(home, F1, "--no-header", "--inherited")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.index("trunk question 0") < out.index("forked here") < out.index("branch one question 0")
    assert "trunk later" not in out                    # the parent's turns after the fork are not this session's


def test_a_fork_whose_parent_is_gone_shows_what_it_inherited(tmp_path):
    home, _ = family(tmp_path, with_parent=False)
    r = run_cli(home, F1, "--no-header")
    assert r.returncode == 0, r.stderr
    assert "trunk question 0" in r.stdout and "branch one question 0" in r.stdout
    assert "not found" in r.stdout


def test_reading_the_parent_lists_its_forks_and_shows_none_of_their_turns(tmp_path):
    home, _ = family(tmp_path)
    r = run_cli(home, P, "--no-header")
    assert r.returncode == 0, r.stderr
    assert "trunk later question 2" in r.stdout
    body = r.stdout.split("\n### ", 1)[1]
    assert "branch one question" not in body and "branch two question" not in body
    head = r.stdout.split("\n### ", 1)[0]
    assert F1 in head and F2 in head and "Forked into" in head and "--family" in head


def test_a_fork_prefix_finds_the_fork(tmp_path):
    home, _ = family(tmp_path)
    r = run_cli(home, F1[:8], "--no-header")
    assert r.returncode == 0, r.stderr
    body = r.stdout.split("\n### ", 1)[1]
    assert "branch one question 0" in body and "trunk question 0" not in body


def test_the_span_of_a_fork_starts_at_its_own_turns(tmp_path):
    home, _ = family(tmp_path)
    r = run_cli(home, F1, "--no-header")
    span = [l for l in r.stdout.splitlines() if l.startswith("**Span:**")][0]
    assert ts(12)[:16] in span and ts(0)[:16] not in span


# --- reading a whole family -----------------------------------------------------------------

def test_family_reads_the_trunk_once_and_each_branch_once(tmp_path):
    home, _ = family(tmp_path, nested=True)
    r = run_cli(home, "--family", F2, "--no-header")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for k in range(5):
        assert out.count(f"trunk question {k}\n") == 1
    for k in range(4):
        assert out.count(f"branch one question {k}\n") == 1
    for k in range(2):
        assert out.count(f"branch two question {k}\n") == 1
        assert out.count(f"branch three question {k}\n") == 1
    # the trunk goes first; each branch follows its own heading, a child after its parent
    assert out.index("trunk later question 2") < out.index("⑂ Branch: Branch one") < out.index("branch one question 0")
    assert out.index("branch one question 0") < out.index("⑂ Branch: Branch three") < out.index("branch three question 0")
    assert out.index("⑂ Branch: Branch one") < out.index("⑂ Branch: Branch three") < out.index("⑂ Branch: Branch two")
    assert out.count("branches off here") >= 2


# --- listing and the digest -----------------------------------------------------------------

def test_list_says_which_sessions_are_forks(tmp_path):
    home, _ = family(tmp_path)
    rows = json.loads(run_cli(home, "--list", "--json", "--all-projects").stdout)
    by_id = {r["id"]: r for r in rows}
    assert by_id[F1]["forked_from"] == P and by_id[F2]["forked_from"] == P
    assert by_id[P]["forked_from"] is None


def test_the_digest_does_not_repeat_the_parents_messages_under_a_fork(tmp_path):
    _, proj = family(tmp_path)
    got = cd.user_messages_since([str(proj / f"{F1}.jsonl")], None, 50)
    assert got == [f"branch one question {k}" for k in range(4)]
