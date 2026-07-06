# ccdistill

**Distill a [Claude Code](https://code.claude.com) session transcript into a lean resume brief.**

Resuming a large, stale session with `claude --resume` reprocesses the *entire*
transcript as fresh input on the first turn — one of the most expensive requests you
can send, spent mostly re-reading tool-output noise you'll ignore. `ccdistill` strips a
session's JSONL down to signal — the human and assistant messages, the assistant's
reasoning, and *which tools were called and why* — and drops the bulky tool **outputs**,
injected system files, and reminders. Hand the result to a **fresh** session and it
reconstructs "what were we doing / decided / left unresolved / next step" cheaply, then
verifies against your live repo before acting.

Typical reduction: **~60× smaller** (a sampled 521 KB session → 8 KB, ~2K tokens).

It is deterministic — **no LLM call, no API key.** The fresh session does the
reconstruction as its first turn (a turn you were going to send anyway), so it's
effectively free.

## Why

Claude Code's prompt cache is prefix-based with a short TTL. Come back after the cache
expires and the first resumed turn re-processes the whole conversation at full input
price. On a long session that single turn can dominate the entire session's cost — and
most of what it re-reads is stale tool output the model will discard. `ccdistill` flips
this: you pay the cold first turn on a *tiny* payload instead of a huge noisy one.

A distilled brief is also a better resume payload than the raw transcript: stale tool
outputs (directory listings, file dumps, old test runs) actively mislead a resuming
agent, so removing them is a feature, not just a size win.

## Install

Requires Python 3.8+. No dependencies.

```bash
curl -fsSL https://raw.githubusercontent.com/semikolon/ccdistill/main/ccdistill \
  -o ~/.local/bin/ccdistill && chmod +x ~/.local/bin/ccdistill
```

(or clone and symlink `ccdistill` onto your `PATH`).

## Usage

```bash
ccdistill                     # newest session in the current dir's project
ccdistill <session-id>        # a specific session (searched across all roots)
ccdistill path/to.jsonl       # a specific transcript file
ccdistill --list [--json]     # discover recent sessions (non-interactive)
ccdistill --pick              # interactive numbered picker (human terminal)
ccdistill --project <name>    # newest session in a named project
```

**The resume workflow (after a crash / power-cut / long gap):**

```bash
ccdistill | pbcopy            # newest session in this project → clipboard
# open a fresh `claude`, paste, hit enter → it reconstructs + verifies the repo
```

or write it to a file:

```bash
ccdistill -o /tmp/handoff.md
# in a fresh claude: "read /tmp/handoff.md and continue"
```

The brief is prefixed with a header instructing the fresh session to establish ground
truth from the repo first (`git status` / `git log` / `git diff` / run tests) and let
the **repo win** on any contradiction with the (possibly stale) brief.

### For AI agents / scripts (non-interactive)

`--pick` needs a human terminal (it reads a choice from stdin) and errors out under a
non-tty rather than hanging. Agents discover sessions instead with `--list`:

```bash
ccdistill --list --json       # [{id, modified, project, size_kb, title, ...}]
```

Pick an `id` (the `size_kb` field hints how substantial a session is — skip a tiny
just-started one, e.g. the agent's own current session), then `ccdistill <id>`.

## What it keeps vs drops

| Kept | Dropped |
|------|---------|
| Human + assistant natural-language text | Tool **outputs** (`tool_result` bodies) |
| Assistant reasoning (`thinking`, truncated) | Injected `CLAUDE.md` / project system files |
| Each `tool_use` as a one-line intent stub (`⏺ Bash: cargo test`) | `<system-reminder>` and metadata line-types |
| Repo-verify resume header + session metadata | Subagent/sidechain turns, compaction summaries |

It handles the **compaction chain** (a session split across multiple files sharing one
`sessionId`) and resolves sessions by `sessionId` + newest mtime, dodging Claude Code's
stale-transcript-path-on-continued-sessions behavior.

## Options

```
-o FILE            write to FILE instead of stdout
--list             list recent sessions, machine-readable (id/time/size/project/title)
--json             with --list: JSON instead of TSV
--limit N          with --list: max rows (default 20)
--pick             interactive picker (human terminal only)
--project NAME     select within a named project instead of cwd
--all-projects     with --list/--pick, span every project
--no-thinking      drop assistant reasoning blocks
--thinking-chars N truncate each thinking block to N chars (default 700)
--no-header        omit the repo-verify resume header
--raw              transcript only (no header, no metadata)
```

### Environment

```
CCDISTILL_USER_LABEL     label for the human's turns (default: User)
CCDISTILL_ARCHIVE_ROOTS  extra transcript roots (os.pathsep-separated) beyond
                         ~/.claude/projects — e.g. an archive volume
```

## Compatibility

Claude Code's JSONL session format is internal and can change between versions. This
parser is defensive (it only touches `user`/`assistant` messages and known block types,
ignoring anything else) and warns on stderr when it sees a Claude Code version outside
the tested `2.x` line. Tested against Claude Code 2.x.

## How it fits

Sibling to the built-ins and other tools: use `claude --resume` when the cache is still
warm (recent, same directory/model); use `/compact` or `/recap` while you're still *in*
a session. `ccdistill` is for the **from-disk, session-is-dead** case — a crash, a power
cut, or a session gone cold — where resuming raw is expensive and you want the signal
without the noise.

## Authors

Built together by **Fredrik Bränström** and **Claude** (Anthropic).

## License

[MIT](LICENSE)
