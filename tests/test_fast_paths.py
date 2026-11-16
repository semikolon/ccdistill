"""Reading a transcript from its end, the byte prefilter, and the digest of other sessions.

The transcripts are generated here in the shape Claude Code writes (compact JSON, one object a
line, user/assistant turns among attachment, API-request, snapshot, sidechain and compaction
lines), with the awkward cases on purpose: multi-byte text at block edges, a line longer than
a block, blank and broken lines, no final newline, timestamps slightly out of order, and a
compaction chain whose middle file holds the window. The oracle for every fast path is the
slow path of the same program: `distill` over everything, then a plain filter.
Run: python3 -m pytest tests/"""

import importlib.machinery
import importlib.util
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ccdistill"


def load():
    loader = importlib.machinery.SourceFileLoader("ccdistill_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("ccdistill_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cd = load()

BASE = datetime(2026, 11, 14, 8, 0, 0)


def ts(i):
    return (BASE + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jl(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def common(sid, i):
    return {"uuid": f"id{i}", "timestamp": ts(i), "sessionId": sid, "version": "2.1.200",
            "gitBranch": "main", "cwd": "/work"}


def user(sid, i, content, **extra):
    return jl({"parentUuid": None, "isSidechain": extra.pop("isSidechain", False), "type": "user",
               "message": {"role": "user", "content": content}, **common(sid, i), **extra})


def assistant(sid, i, blocks):
    return jl({"parentUuid": None, "isSidechain": False, "message": {"role": "assistant", "content": blocks},
               "type": "assistant", **common(sid, i)})


def tool_result(sid, i, size):
    return user(sid, i, [{"tool_use_id": f"t{i}", "type": "tool_result", "content": "r" * size}])


def attachment(sid, i, size):
    return jl({"parentUuid": None, "isSidechain": False, "attachment": {"type": "hook", "content": "a" * size},
               "type": "attachment", **common(sid, i)})


def blob(sid, i, size):
    # a non-conversation line whose text holds the words of a user line, quotes escaped as JSON does
    return jl({"type": "api-request-blob", "sessionId": sid, "timestamp": ts(i),
               "blob": '{"type":"user","message":"' + "b" * size})


def nested_user_word(sid, i):
    # not a turn, but the raw bytes `"type":"user"` do appear: the prefilter lets it through, the parse drops it
    return jl({"type": "attachment", "attachment": {"type": "user", "note": "x"}, **common(sid, i)})


def scenario(sid, n=240, start=0, seed=1):
    """About n turns' worth of lines, timestamps rising one minute a step."""
    rnd = random.Random(seed)
    lines = [jl({"type": "file-history-snapshot", "messageId": "m", "snapshot": {"trackedFileBackups": {}}})]
    for k in range(n):
        i = start + k
        kind = k % 8
        if kind == 0:
            lines.append(user(sid, i, f"Fråga {k}: räkna på åäö och 🙂 och 日本語 {'x' * rnd.randint(0, 40)}"))
        elif kind == 1:
            lines.append(assistant(sid, i, [{"type": "thinking", "thinking": "tänker " * 30},
                                            {"type": "text", "text": f"Svar {k}"}]))
        elif kind == 2:
            lines.append(assistant(sid, i, [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                             "input": {"command": f"ls dir{k}"}}]))
        elif kind == 3:
            lines.append(tool_result(sid, i, rnd.choice([10, 3000, 90000])))
        elif kind == 4:
            lines.append(attachment(sid, i, rnd.choice([50, 20000])))
            lines.append(blob(sid, i, 4000))
            lines.append(nested_user_word(sid, i))
        elif kind == 5:
            lines.append(user(sid, i, [{"type": "text", "text": f"Blocktext {k}"},
                                       {"type": "image", "source": {"type": "base64", "data": "I" * 5000}}]))
        elif kind == 6:
            lines.append(user(sid, i, f"sidokedja {k}", isSidechain=True))
            lines.append(user(sid, i, "Hidden summary", isCompactSummary=True))
        else:
            lines.append(user(sid, i, "<system-reminder>not the human</system-reminder>"))
            lines.append(jl({"type": "custom-title", "customTitle": f"t{k}", "sessionId": sid}))
    return lines


def write(path, lines, final_newline=True):
    text = "\n".join(lines) + ("\n" if final_newline else "")
    Path(path).write_bytes(text.encode("utf-8"))
    return str(path)


def slow(chain, lo=None, hi=None, last=None, **kw):
    """The oracle: every turn of the chain, then the plain window."""
    turns = cd.distill(chain, **kw)
    if lo or hi:
        turns = [t for t in turns if (not t[2]) or ((not lo or t[2] >= lo) and (not hi or t[2] <= hi))]
    return turns[-last:] if last else turns


# --- the reverse reader -------------------------------------------------------------------

@pytest.mark.parametrize("block", [1, 2, 3, 7, 64, 1000, 262144])
def test_reversed_lines_equal_the_forward_lines_reversed(tmp_path, block):
    lines = ["å" * 5, "", "plain", "🙂🙂🙂", "x" * 300, "日本語 text", "last"]
    for final_newline in (True, False):
        p = write(tmp_path / "f.jsonl", lines, final_newline)
        want = [l.encode("utf-8") for l in reversed(lines) if l]
        assert list(cd._lines_reversed(p, block=block)) == want


def test_reversed_lines_of_nothing_and_of_one_line(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_bytes(b"")
    assert list(cd._lines_reversed(str(p))) == []
    p.write_bytes(b"only")
    assert list(cd._lines_reversed(str(p), block=2)) == [b"only"]
    p.write_bytes(b"\n\n")
    assert list(cd._lines_reversed(str(p))) == []


def test_a_line_longer_than_many_blocks_comes_out_whole(tmp_path):
    big = "z" * 100_000
    p = write(tmp_path / "f.jsonl", ["first", big, "last"])
    assert list(cd._lines_reversed(p, block=4096)) == [b"last", big.encode(), b"first"]


def test_crlf_lines_keep_their_carriage_return_and_still_parse(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_bytes(b'{"type":"user","message":{"content":"hej"},"timestamp":"2026-11-14T08:00:00Z"}\r\n')
    (line,) = list(cd._lines_reversed(str(p)))
    assert cd._json_line(line)["message"]["content"] == "hej"


U, A = b'"type":"user"', b'"type":"assistant"'


@pytest.mark.parametrize("final_newline", [True, False])
@pytest.mark.parametrize("markers", [(U,), (A,), (U, A)])
def test_marked_lines_come_back_as_the_filtered_reversed_lines(tmp_path, markers, final_newline):
    lines = ['{"type":"user","a":1}', "", "noise", '{"x":1,"type":"user","y":{"type":"user"}}',
             'å{"type":"assistant"}', "tail 🙂", "z" * 70_000 + '{"type":"user"}', '{"type":"user"}']
    p = write(tmp_path / "f.jsonl", lines, final_newline)
    want = [l.encode("utf-8") for l in reversed(lines) if l and any(m in l.encode("utf-8") for m in markers)]
    assert list(cd._marked_lines_reversed(p, markers)) == want


def test_marked_lines_of_nothing_and_of_a_file_without_a_marker(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_bytes(b"")
    assert list(cd._marked_lines_reversed(str(p), (U,))) == []
    p.write_bytes(b"no marker here\nnor here\n")
    assert list(cd._marked_lines_reversed(str(p), (U, A))) == []
    assert list(cd._marked_lines_reversed(str(tmp_path / "absent.jsonl"), (U,))) == []


def test_marked_lines_equal_the_lines_of_the_slow_reader_on_a_whole_session(tmp_path):
    p = write(tmp_path / "s.jsonl", scenario("s1", n=160))
    slow_lines = [l for l in cd._lines_reversed(p) if U in l or A in l]
    assert list(cd._marked_lines_reversed(p, (U, A))) == slow_lines and len(slow_lines) > 100


# --- the byte prefilter -------------------------------------------------------------------

def test_the_prefilter_changes_nothing_in_what_distill_returns(tmp_path):
    p = write(tmp_path / "s.jsonl", scenario("s1"))
    assert cd._compact_json(p)
    filtered = cd.distill([p])
    reference = []
    for raw in open(p, "rb"):
        o = cd._json_line(raw)
        t = cd._turn_of(o) if o else None
        if t:
            reference.append(t)
    assert filtered == reference and len(filtered) > 100


def test_spaced_json_is_read_without_the_byte_test(tmp_path):
    lines = [json.dumps(json.loads(l)) for l in scenario("s1", n=40)]       # a space after each colon
    p = write(tmp_path / "s.jsonl", lines)
    assert not cd._compact_json(p)
    assert cd.distill([p]) == cd.distill([write(tmp_path / "c.jsonl", scenario("s1", n=40))])


def test_non_conversation_lines_are_not_candidates_and_conversation_lines_are():
    assert not cd._candidate(blob("s", 1, 100).encode())
    assert not cd._candidate(attachment("s", 1, 100).encode())
    assert cd._candidate(user("s", 1, "hej").encode())
    assert cd._candidate(assistant("s", 1, [{"type": "text", "text": "hej"}]).encode())
    assert cd._candidate(nested_user_word("s", 1).encode())          # let through, dropped by the parse


# --- windows read from the end ------------------------------------------------------------

LO = [None, ts(0), ts(40), ts(120), ts(239), ts(500)]
HI = [None, ts(30), ts(100), ts(500)]
LAST = [None, 1, 7, 60, 10_000]


@pytest.fixture(scope="module")
def session_file(tmp_path_factory):
    return write(tmp_path_factory.mktemp("session") / "s.jsonl", scenario("s1"))


@pytest.mark.parametrize("lo", LO)
@pytest.mark.parametrize("hi", HI)
@pytest.mark.parametrize("last", LAST)
def test_a_window_from_the_end_equals_the_slow_window(session_file, lo, hi, last):
    p = session_file
    lo_s, hi_s = (lo or "")[:19] or None, (hi or "")[:19] or None
    for level in ("full", "user"):
        got = cd.distill_tail([p], lo=lo_s, hi=hi_s, last=last, level=level)
        assert got == slow([p], lo=lo_s, hi=hi_s, last=last, level=level)


def test_timestamps_a_little_out_of_order_do_not_lose_turns(tmp_path):
    lines = scenario("s1")
    rnd = random.Random(3)
    for _ in range(40):                      # swap neighbouring timestamps, as concurrent writes do
        a = rnd.randrange(1, len(lines) - 4)
        la, lb = json.loads(lines[a]), json.loads(lines[a + 3])
        if "timestamp" in la and "timestamp" in lb:
            la["timestamp"], lb["timestamp"] = lb["timestamp"], la["timestamp"]
            lines[a], lines[a + 3] = jl(la), jl(lb)
    p = write(tmp_path / "s.jsonl", lines)
    for lo in (ts(30), ts(100), ts(200)):
        assert cd.distill_tail([p], lo=lo[:19]) == slow([p], lo=lo[:19])


def test_a_window_inside_a_middle_file_of_a_chain_is_found(tmp_path):
    """The real failure of the first version: the newest file of a chain began with a long
    replay of old turns, the reader gave up there and never reached the file holding the window."""
    old = write(tmp_path / "a.jsonl", scenario("s1", n=120, start=0))
    middle = write(tmp_path / "b.jsonl", scenario("s1", n=120, start=100, seed=2))
    replay = scenario("s1", n=400, start=0, seed=3) + scenario("s1", n=16, start=400, seed=4)
    newest = write(tmp_path / "c.jsonl", replay)
    chain = [old, middle, newest]
    lo = ts(110)[:19]
    got = cd.distill_tail(chain, lo=lo)
    assert got == slow(chain, lo=lo)
    assert any(t[2] < ts(230)[:19] for t in got)     # turns from the middle file are in it


def test_a_file_wholly_older_than_the_window_is_not_read(tmp_path, monkeypatch):
    old = write(tmp_path / "a.jsonl", scenario("s1", n=800, start=0))
    new = write(tmp_path / "b.jsonl", scenario("s1", n=40, start=2000, seed=5))
    seen = []
    real = cd._lines_backward

    def spy(path):
        seen.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(cd, "_lines_backward", spy)
    cd.distill_tail([old, new], lo=ts(1990)[:19])
    assert seen == ["b.jsonl"]


def test_the_end_of_a_large_file_is_parsed_without_the_file(tmp_path, monkeypatch):
    p = write(tmp_path / "big.jsonl", scenario("s1", n=2400))
    size = os.path.getsize(p)
    parsed = {"bytes": 0}
    real = cd._json_line

    def spy(raw):
        parsed["bytes"] += len(raw)
        return real(raw)

    monkeypatch.setattr(cd, "_json_line", spy)
    got = cd.distill_tail([p], last=5)
    assert len(got) == 5
    assert size > 3_000_000
    assert parsed["bytes"] < size / 20, (parsed["bytes"], size)


def test_the_last_timestamp_is_read_from_the_end_in_blocks(tmp_path, monkeypatch):
    p = write(tmp_path / "big.jsonl", scenario("s1", n=2400))
    size = os.path.getsize(p)
    read = {"bytes": 0}

    class Counting:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.fh.close()

        def seek(self, *a):
            return self.fh.seek(*a)

        def tell(self):
            return self.fh.tell()

        def read(self, n=-1):
            data = self.fh.read(n)
            read["bytes"] += len(data)
            return data

    monkeypatch.setattr(cd, "open", lambda path, mode="r", **kw: Counting(open(path, mode, **kw)), raising=False)
    assert cd._last_timestamp(p) == ts(2399)[:19]
    assert read["bytes"] < size / 4, (read["bytes"], size)


# --- the span of a session -------------------------------------------------------------------

def test_span_and_branch_come_from_the_head_and_the_tail(tmp_path):
    p = write(tmp_path / "s.jsonl", scenario("s1", n=80))
    first, last, branch = cd._span_and_branch([p])
    stamps = [json.loads(l).get("timestamp") for l in open(p) if json.loads(l).get("timestamp")]
    assert (first, last, branch) == (stamps[0][:16], stamps[-1][:16], "main")


def test_the_span_of_a_chain_ends_at_its_last_file(tmp_path):
    a = write(tmp_path / "a.jsonl", scenario("s1", n=30, start=0))
    b = write(tmp_path / "b.jsonl", scenario("s1", n=30, start=500))
    first, last, _ = cd._span_and_branch([a, b])
    assert first == ts(0)[:16] and last == ts(529)[:16]


def test_no_branch_and_no_timestamps_give_empty_strings(tmp_path):
    p = write(tmp_path / "s.jsonl", ['{"type":"user","message":{"content":"x"}}'])
    assert cd._span_and_branch([p]) == ("", "", "")
    assert cd._span_and_branch([]) == ("", "", "")


# --- sessions found by id -------------------------------------------------------------------

SID = "0b9c2c6e-47b0-4c3a-9f1d-8d2a1f6c1e11"


def test_a_chain_is_found_among_other_sessions_files(tmp_path):
    proj = tmp_path / "-work-proj"
    proj.mkdir()
    write(proj / f"{SID}.jsonl", scenario(SID, n=24))
    other = "7f6e5d4c-3b2a-4190-8877-665544332211"
    for k in range(5):
        write(proj / f"{other[:-1]}{k}.jsonl", scenario(other, n=24))
    # a chain file with another name, same session id
    write(proj / "aaaaaaaa-0000-0000-0000-000000000000.jsonl", scenario(SID, n=10, start=500))
    chain = cd._chain_for_session(SID, [proj])
    assert sorted(Path(c).name for c in chain) == sorted([f"{SID}.jsonl", "aaaaaaaa-0000-0000-0000-000000000000.jsonl"])


def test_a_file_whose_head_names_no_session_id_is_still_looked_at(tmp_path):
    proj = tmp_path / "-work-proj"
    proj.mkdir()
    lines = [jl({"type": "file-history-snapshot", "snapshot": {"pad": "p" * 80_000}})] + scenario(SID, n=8)
    write(proj / "bbbbbbbb-0000-0000-0000-000000000000.jsonl", lines)
    assert [Path(c).name for c in cd._chain_for_session(SID, [proj])] == ["bbbbbbbb-0000-0000-0000-000000000000.jsonl"]


# --- the digest of other sessions ------------------------------------------------------------

def test_user_messages_since_are_the_last_n_in_order_without_the_noise(tmp_path):
    lines = [user("s", i, f"meddelande {i}") for i in range(10)]
    lines.insert(5, user("s", 5, "[keepwarm ping] Keepalive for this session's cache"))
    lines.insert(6, user("s", 6, "# Persist Session Knowledge to Docs\n\nPurpose"))
    lines.insert(7, user("s", 7, [{"type": "text", "text": "Base directory for this skill: /x"}]))
    lines.append(tool_result("s", 11, 500))
    p = write(tmp_path / "s.jsonl", lines)
    assert cd.user_messages_since([p], None, 3) == ["meddelande 7", "meddelande 8", "meddelande 9"]
    got = cd.user_messages_since([p], ts(4)[:19], 50)
    assert got == [f"meddelande {i}" for i in range(4, 10)]


def test_user_messages_are_one_line_each(tmp_path):
    p = write(tmp_path / "s.jsonl", [user("s", 1, "rad ett\n\nrad   två\n")])
    assert cd.user_messages_since([p], None, 5) == ["rad ett rad två"]


def make_home(tmp_path, sessions):
    """sessions: [(project dir name, session id, turns, minutes_ago, title)] under a throwaway HOME."""
    home = tmp_path / "home"
    root = home / ".claude" / "projects"
    now = datetime.now()
    for proj, sid, n, ago, _ in sessions:
        d = root / proj
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        lines = [user(sid, 0, "Öppningsfrågan om något")]
        base = now - timedelta(minutes=ago)
        for k in range(n):
            o = json.loads(user(sid, k, f"{proj} meddelande {k} med åäö"))
            o["timestamp"] = (base + timedelta(minutes=k)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            lines.append(jl(o))
        write(f, lines)
        stamp = (now - timedelta(minutes=ago - n)).timestamp()
        os.utime(f, (stamp, stamp))
    return home


def run_cli(home, *args):
    env = dict(os.environ, HOME=str(home))
    env.pop("CCDISTILL_ARCHIVE_ROOTS", None)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


def test_digest_lists_other_sessions_since_the_hour_newest_first(tmp_path):
    ids = ["11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
           "33333333-cccc-4ccc-8ccc-cccccccccccc", "44444444-dddd-4ddd-8ddd-dddddddddddd"]
    home = make_home(tmp_path, [
        ("-work-alpha", ids[0], 50, 300, ""), ("-work-beta", ids[1], 50, 120, ""),
        ("-work-gamma", ids[2], 50, 3000, ""), ("-work-me", ids[3], 50, 30, "")])
    r = run_cli(home, "--digest", "--hours", "6", "--exclude", ids[3])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.splitlines()[0].endswith(": 2")                       # alpha and beta; gamma is older, me is excluded
    assert out.index("beta ·") < out.index("alpha ·")
    assert "gamma" not in out and "me ·" not in out
    assert "  - -work-beta meddelande 49 med åäö" in out and "-work-beta meddelande 42 med åäö" in out
    assert "-work-beta meddelande 41 med" not in out                 # eight messages per session


def test_digest_respects_the_hard_cap_on_whole_sessions(tmp_path):
    specs = [("-work-p%d" % k, f"{k:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 60, 100 + k, "") for k in range(12)]
    home = make_home(tmp_path, specs)
    r = run_cli(home, "--digest", "--hours", "6", "--cap", "1500", "--sessions", "12")
    assert r.returncode == 0, r.stderr
    body = r.stdout.split("\n", 1)[1]
    assert len(body) <= 1500 + 200
    assert "more sessions in the window, cut at the cap" in body
    assert body.count(" · last ") >= 1


def write_titles(home, titles):
    d = home / "Library" / "Application Support" / "Claude" / "claude-code-sessions" / "acct" / "org"
    d.mkdir(parents=True, exist_ok=True)
    for k, (sid, title) in enumerate(titles.items()):
        (d / f"local_{k}.json").write_text(json.dumps({"cliSessionId": sid, "title": title}), encoding="utf-8")
    (d / "local_broken.json").write_text("{not json " + next(iter(titles)), encoding="utf-8")


def test_titles_for_some_sessions_equal_the_titles_of_all_for_those_ids(tmp_path, monkeypatch):
    write_titles(tmp_path, {"id-a": "Rubrik  med\n mellanrum", "id-b": "Annan rubrik", "id-c": "Tredje"})
    monkeypatch.setattr(cd, "_CCD_SESSIONS_ROOT", tmp_path / "Library" / "Application Support" / "Claude" / "claude-code-sessions")
    monkeypatch.setattr(cd, "_ccd_titles_cache", None)
    everything = cd._ccd_titles()
    some = cd._ccd_titles_for({"id-a", "id-c", "id-not-there"})
    assert some == {"id-a": "Rubrik med mellanrum", "id-c": "Tredje"}
    assert all(everything[k] == v for k, v in some.items())
    assert cd._ccd_titles_for(set()) == {}


def test_digest_and_list_show_the_title_the_app_gave_the_session(tmp_path):
    sid = "66666666-ffff-4fff-8fff-ffffffffffff"
    home = make_home(tmp_path, [("-work-titled", sid, 40, 90, "")])
    write_titles(home, {sid: "Tre ämnen · Ett till · Sist"})
    r = run_cli(home, "--digest", "--hours", "6")
    assert r.returncode == 0 and "titled · Tre ämnen · Ett till · Sist · last " in r.stdout
    listed = json.loads(run_cli(home, "--list", "--json", "--all-projects").stdout)
    assert listed[0]["title"] == "Tre ämnen · Ett till · Sist"


def test_digest_with_nothing_in_the_window_says_so(tmp_path):
    home = make_home(tmp_path, [("-work-old", "55555555-eeee-4eee-8eee-eeeeeeeeeeee", 40, 5000, "")])
    r = run_cli(home, "--digest", "--hours", "1")
    assert r.returncode == 0 and r.stdout.splitlines()[0].endswith(": 0")


# --- the command line, whole -----------------------------------------------------------------

def test_since_and_last_say_that_the_total_is_not_counted_and_full_pass_counts_it(tmp_path):
    p = write(tmp_path / "s.jsonl", scenario("s1", n=80))
    fast = subprocess.run([sys.executable, str(SCRIPT), p, "--no-header", "--last", "6"], capture_output=True, text=True)
    full = subprocess.run([sys.executable, str(SCRIPT), p, "--no-header", "--last", "6", "--full-pass"],
                          capture_output=True, text=True)
    assert fast.returncode == 0 and full.returncode == 0
    assert "6 turns, read from the end of the session" in fast.stdout
    assert "6 of " in full.stdout
    body = lambda s: s.split("anything outside it is unread.\n", 1)[1]
    assert body(fast.stdout) == body(full.stdout)                    # the same turns either way


def test_an_empty_window_is_an_error_not_an_empty_brief(tmp_path):
    p = write(tmp_path / "s.jsonl", scenario("s1", n=20))
    r = subprocess.run([sys.executable, str(SCRIPT), p, "--since", "2030-01-01"], capture_output=True, text=True)
    assert r.returncode == 1 and "window contains no turns" in r.stderr
