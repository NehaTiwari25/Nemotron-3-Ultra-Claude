# Claude context handoff

When Claude Code fills its context it compacts, and what survives is a generic summary. This replaces that with a purpose-built continuation brief written by **Nemotron 3 Ultra** reading the full session transcript — what the session was trying to do, what it established, what to do next, and which details cost time.

Part of [Second Opinion](../README.md); it reuses that server's OpenRouter key and needs no configuration of its own beyond the allowlist below.

## How it hooks in

```
PreCompact   (auto, manual)            -> handoff.py capture   hand off: write the brief
SessionStart (compact, startup, resume) -> handoff.py inject    hand back: brief + work done
```

`SessionStart` stdout is added to Claude's context, which is what closes the loop. There is **no "context nearly full" hook** — compaction is the only lifecycle point, so the brief is written *at* the boundary, not before it.

The hand-back carries two things:

1. **The brief**, so the model on the other side resumes where the work was rather than where a generic summary left it.
2. **Anything `delegate.py` did in the meantime** — which file changed, how many edits, whether the tests passed, and whether it was left applied or reverted. Files can change while Claude has no context; discovering that later in an unexpected diff is worse than being told.

Both are delivered **once**. A brief that reappears at every session start is noise, and an announcement that repeats is an announcement that gets skipped — which matters most for the part reporting unreviewed changes. A *new* brief or a *new* delegated task is announced normally; the old one is not repeated.

`startup` and `resume` are registered alongside `compact` so work done between sessions is reported when you come back, not just after a compaction.

## Projects are opt in

`~/.claude/handoff/config.json`:

```json
{ "allowed_projects": ["C:\\path\\to\\a\\project", "/home/you/another"] }
```

A transcript contains everything the session touched — source, file contents, command output. Sending that to a third-party endpoint is a per-project decision, made once, deliberately. **Unlisted projects never reach the network**; they still get a local brief.

This is not a general precaution. Client work and bounty-scope code must not go through the free tier at all, and a hook fires automatically — so the default is deny, and a prefix match on a sibling directory (`foo-other` vs `foo`) does not count as a match.

## It works without the API

The brief is assembled locally first — opening request, later instructions, files edited, recent commands, last message — then Nemotron *upgrades* it. The API half runs on a 50-requests-a-day free tier that is usually already spent on dataset generation, so anything that only works when quota is free usually does not work. Quota exhausted, offline, slow, or timed out all produce the same outcome: the local brief, silently.

Each compaction costs **one** request.

## Things that had to be learned the hard way

- **A long transcript reads as something to continue.** With the instruction only in the system prompt, the model echoed the session back verbatim and carried on answering its last message. Fencing the transcript in `<transcript>` and putting the ask *after* it fixed that — at this length, recency beats the system prompt.
- **Do not prefill the reply.** Seeding it with `## Goal` looks helpful and ends the turn after ~70 tokens: one section, the next heading, stop. Removing it took the note from 76 tokens to 771.
- **Never block compaction.** Every path exits 0, the API call has a hard timeout, and the body is wrapped. A hook that fails must not break the session it exists to help.
- **cp1252 will eat the output.** The brief is full of em dashes; printing one to a Windows console raises, the catch-all swallows it, and the hook exits 0 having delivered nothing. stdout is reconfigured to UTF-8 first.

## Delegation, and why it retries

`delegate.py` gives Nemotron the brief, a failing test, and one file, and takes back find/replace edits. They're applied all-or-nothing, then the test suite decides.

```bash
python delegate.py --task "..." --file handoff.py --test "python test_handoff.py"
```

It retries up to `--attempts` (default 3), and the retry is where the value is: a first patch that applies cleanly but fixes the wrong thing is the common case, and it's only recoverable if the model is told what happened. Each failure goes back with the edits it tried, whether they applied, which checks it fixed, and which still fail.

Two guards stop the loop wasting your allowance:

- **Every attempt starts from the original file.** Stacking a second patch on a failed first one compounds a mistake instead of replacing it — and the model wrote its anchors against the original.
- **An identical patch ends the loop.** The same edits fail the same way; paying a second request to watch that happen is the one thing a retry loop must never do.

One non-obvious detail lives in `run()`: the test command executes with a throwaway bytecode cache. Python invalidates a `.pyc` by size and mtime, and a find/replace swapping one operator for another — `a - b` for `a + b`, the archetypal minimal fix — leaves the size identical while the retry loop rewrites the file within the same second. The suite then runs against **stale bytecode**, a correct patch is reported as a failure, and the model is sent hunting a bug that isn't there. Costs the whole retry budget, and there's nothing in the output to suggest why.

It is still not an agent: no shell, no file discovery, one file, one action. Widening that is what makes a weak model dangerous — once it can edit the tests, the tests stop being a gate.

## Testing

```bash
python test_handoff.py                       # 67 tests, no network, no API key
python test_delegate.py                      # 22 tests, model stubbed
python handoff.py test <transcript.jsonl>    # print a brief from a real transcript
```

Transcripts live in `~/.claude/projects/<slug>/*.jsonl`.

The suite is mostly failure cases, because this runs unattended in someone's session: garbage on stdin, a missing transcript, a half-parseable one, a stale brief, a denied project, and unicode through a real subprocess pipe. Every one asserts **exit code 0** — exit 2 would block compaction, turning a broken helper into a broken session.

### Proving the hook actually fires

The tests cover the script; they cannot cover Claude Code invoking it. `PreCompact` is registered for **both** `auto` and `manual`, so:

1. Run `/compact` in an allowlisted project.
2. Check `~/.claude/handoff/` for a fresh `.md` matching that project.
3. The brief is injected on the session that follows.

If nothing appears, open `/hooks` once — the settings watcher only picks up directories that had a settings file when the session started.

## Install

```bash
git clone https://github.com/NehaTiwari25/Nemotron-3-Ultra-Claude
cd Nemotron-3-Ultra-Claude/handoff
python test_handoff.py
```

Then add to `~/.claude/settings.json` (exec form, so Windows backslashes never reach a shell parser):

```json
{
  "hooks": {
    "PreCompact": [
      { "matcher": "auto",   "hooks": [{ "type": "command", "command": "python", "args": ["<path>/handoff.py", "capture"], "timeout": 90 }] },
      { "matcher": "manual", "hooks": [{ "type": "command", "command": "python", "args": ["<path>/handoff.py", "capture"], "timeout": 90 }] }
    ],
    "SessionStart": [
      { "matcher": "compact", "hooks": [{ "type": "command", "command": "python", "args": ["<path>/handoff.py", "inject"], "timeout": 15 }] },
      { "matcher": "startup", "hooks": [{ "type": "command", "command": "python", "args": ["<path>/handoff.py", "inject"], "timeout": 15 }] },
      { "matcher": "resume",  "hooks": [{ "type": "command", "command": "python", "args": ["<path>/handoff.py", "inject"], "timeout": 15 }] }
    ]
  }
}
```

`auto` and `manual` are separate entries on purpose — whether the matcher is a regex or an exact string is undocumented, and `"auto|manual"` would silently match neither if it is exact.

Requires Python 3.10+ and nothing else. No dependencies.

MIT, under the [repository licence](../LICENSE).
