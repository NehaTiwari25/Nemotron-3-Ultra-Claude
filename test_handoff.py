"""
Tests for the handoff hook. No network, no API key needed.

The interesting cases are all failure cases. This hook runs unattended inside
someone's session, so what matters is not that it works on a good day - it is
that a bad day produces a degraded brief and exit code 0, rather than a stalled
compaction or a silent nothing.

    python test_handoff.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

import handoff

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  {'pass' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not condition else ""))


def transcript(rows: list[dict]) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for row in rows:
        handle.write(json.dumps(row) + "\n")
    handle.close()
    return handle.name


def user(text: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def assistant(text: str = "", tool: tuple[str, dict] | None = None) -> dict:
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    if tool:
        blocks.append({"type": "tool_use", "name": tool[0], "input": tool[1]})
    return {"type": "assistant", "message": {"content": blocks}}


# --- the privacy gate ------------------------------------------------------

def test_allowlist() -> None:
    print("\nallowlist (default deny)")
    config = {"allowed_projects": [r"C:\work\mine"]}
    cases = [
        (r"C:\work\mine", True, "exact"),
        (r"C:\work\mine\sub\deep", True, "subdirectory"),
        (r"c:\WORK\MINE", True, "case-insensitive"),
        (r"C:\work\mine-other", False, "sibling sharing a prefix"),
        (r"C:\work\elsewhere", False, "unrelated"),
        ("", False, "empty cwd"),
    ]
    for cwd, expected, label in cases:
        check(f"{label}: {'allow' if expected else 'deny'}",
              handoff.project_allowed(cwd, config) is expected)
    check("empty allowlist denies everything",
          handoff.project_allowed(r"C:\anything", {"allowed_projects": []}) is False)
    check("missing allowlist key denies", handoff.project_allowed(r"C:\anything", {}) is False)


def test_denied_project_never_calls_network() -> None:
    print("\nnetwork is not touched for unlisted projects")
    called = []
    original = handoff.nemotron_brief
    handoff.nemotron_brief = lambda *a, **k: called.append(1)
    try:
        path = transcript([user("do a thing"), assistant("did it")])
        with tempfile.TemporaryDirectory() as store:
            handoff.STORE = Path(store)
            handoff.CONFIG_PATH = Path(store) / "config.json"
            handoff.CONFIG_PATH.write_text(json.dumps({"allowed_projects": []}), encoding="utf-8")
            handoff.capture({"cwd": r"C:\secret\client", "transcript_path": path})
            written = handoff.brief_path(r"C:\secret\client")
            check("no API call made", not called)
            check("brief still written locally", written.exists())
            text = written.read_text(encoding="utf-8")
            check("brief says why it stayed local", "not on the handoff allowlist" in text)
    finally:
        handoff.nemotron_brief = original


# --- transcript parsing ----------------------------------------------------

def test_user_turn_filter() -> None:
    print("\ntelling real instructions from harness noise")
    check("plain instruction counts", handoff.is_real_user_turn(user("add a test")))
    check("system reminder ignored",
          not handoff.is_real_user_turn(user("<system-reminder>context</system-reminder>")))
    check("interrupt notice ignored",
          not handoff.is_real_user_turn(user("[Request interrupted by user]")))
    check("tool result ignored", not handoff.is_real_user_turn(
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}))
    check("empty text ignored", not handoff.is_real_user_turn(user("   ")))
    check("assistant row is not a user turn", not handoff.is_real_user_turn(assistant("hi")))


def test_local_brief() -> None:
    print("\nlocal brief extracts the facts")
    rows = [
        user("build the parser"),
        assistant("starting", tool=("Write", {"file_path": r"C:\p\parser.py"})),
        assistant("", tool=("Edit", {"file_path": r"C:\p\parser.py"})),
        assistant("", tool=("Bash", {"description": "Run the parser tests"})),
        user("[Request interrupted by user]"),
        user("also handle unicode — em dashes"),
        assistant("done for now"),
    ]
    brief = handoff.local_brief(rows)
    check("opening request captured", "build the parser" in brief)
    check("later instruction captured", "handle unicode" in brief)
    check("interrupt notice excluded", "interrupted" not in brief)
    check("edited file listed", "parser.py" in brief)
    check("file listed once, not twice", brief.count(r"C:\p\parser.py") == 1)
    check("command description listed", "Run the parser tests" in brief)
    check("last message captured", "done for now" in brief)


def test_git_branch_in_brief() -> None:
    """Whoever picks the work up needs to know which branch it was on.

    The transcript records `gitBranch` on its rows; the brief currently drops it,
    so a handover can send someone to the right files on the wrong branch.
    """
    print("\ngit branch reaches the brief")
    rows = [
        dict(user("start the feature"), gitBranch="feature/parser"),
        dict(assistant("working"), gitBranch="feature/parser"),
    ]
    brief = handoff.local_brief(rows)
    check("branch name appears", "feature/parser" in brief,
          "brief did not mention the branch")
    check("branch is labelled", "Branch:" in brief, "no 'Branch:' label in brief")

    # The fixture text must not itself contain the word, and the assertion must
    # test for the LABEL rather than a substring. The first version of this used
    # `user("no branch here")` and asserted `"ranch" not in plain` - which the
    # echoed opening request satisfies on its own, failing a correct patch.
    plain = handoff.local_brief([user("nothing to see"), assistant("ok")])
    check("no branch label when transcript has none", "Branch:" not in plain,
          "brief invented a branch section")


def test_malformed_transcripts() -> None:
    print("\nmalformed and missing transcripts")
    check("missing file returns nothing", handoff.read_transcript(r"C:\nope\missing.jsonl") == [])
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    handle.write('{"type":"user"}\nnot json at all\n\n{"type":"assistant"}\n')
    handle.close()
    rows = handoff.read_transcript(handle.name)
    check("skips unparseable lines, keeps good ones", len(rows) == 2, f"got {len(rows)}")
    check("empty transcript yields a brief without crashing",
          isinstance(handoff.local_brief([]), str))


def test_digest_keeps_opening_request() -> None:
    print("\ndigest trimming")
    rows = [user("THE ORIGINAL GOAL")] + [assistant("x" * 500) for _ in range(50)]
    digest = handoff.transcript_digest(rows, 2000)
    check("digest respects the limit", len(digest) < 2600, f"{len(digest)} chars")
    check("opening goal survives trimming", "THE ORIGINAL GOAL" in digest)
    check("trim is signposted", "omitted" in digest)


def test_echo_guard() -> None:
    print("\necho guard on the model's reply")
    echoed = "\n".join(["ASSISTANT: did a thing", "  [tool] Edit a.py"] * 5)
    check("transcript-shaped reply is rejected", handoff.looks_like_echo(echoed) is True)

    real_note = ("## Goal\nShip the parser.\n\n## State\n- tests pass\n\n"
                 "## Next step\nWire it up.\n\n## Hard-won details\n"
                 "- ASSISTANT: appears once here, quoted, and that is fine\n")
    check("a genuine note is not rejected", handoff.looks_like_echo(real_note) is False)
    check("empty reply is not mistaken for an echo", handoff.looks_like_echo("") is False)

    # The guard is what stands between a wasted request and a brief full of
    # replayed session, so prove it is actually wired into the call path.
    import inspect
    check("guard is used by nemotron_brief",
          "looks_like_echo" in inspect.getsource(handoff.nemotron_brief))


# --- inject ----------------------------------------------------------------

def test_inject_freshness() -> None:
    print("\ninject only speaks when it has something current")
    with tempfile.TemporaryDirectory() as store:
        handoff.STORE = Path(store)
        handoff.CONFIG_PATH = Path(store) / "missing.json"
        cwd = r"C:\p\proj"

        out = io.StringIO()
        with redirect_stdout(out):
            handoff.inject({"cwd": cwd})
        check("silent when no brief exists", out.getvalue() == "")

        path = handoff.brief_path(cwd)
        path.write_text("## Goal\nship the thing — with an em dash\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            handoff.inject({"cwd": cwd})
        check("prints a fresh brief", "ship the thing" in out.getvalue())
        check("explains itself to the reader", "compacted" in out.getvalue())

        old = time.time() - 60 * 60 * 24
        os.utime(path, (old, old))
        out = io.StringIO()
        with redirect_stdout(out):
            handoff.inject({"cwd": cwd})
        check("silent when the brief is stale", out.getvalue() == "")


# --- the hook contract -----------------------------------------------------

def test_exit_codes() -> None:
    print("\nexit code is 0 no matter what (2 would block compaction)")
    script = str(Path(__file__).with_name("handoff.py"))
    cases = [
        ("garbage stdin", "not json"),
        ("empty stdin", ""),
        ("valid json, no fields", "{}"),
        ("nonexistent transcript", json.dumps({"cwd": r"C:\x", "transcript_path": r"C:\no\file.jsonl"})),
    ]
    for label, payload in cases:
        for mode in ("capture", "inject"):
            result = subprocess.run([sys.executable, script, mode], input=payload,
                                    capture_output=True, text=True, timeout=120)
            check(f"{mode}: {label} -> exit 0", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr[:120]}")
    result = subprocess.run([sys.executable, script], input="{}",
                            capture_output=True, text=True, timeout=60)
    check("no mode argument -> exit 0", result.returncode == 0)


def test_unicode_survives_the_pipe() -> None:
    print("\nunicode survives a real subprocess pipe (cp1252 trap)")
    script = str(Path(__file__).with_name("handoff.py"))
    with tempfile.TemporaryDirectory() as store:
        brief_dir = Path(store)
        cwd = r"C:\p\unicode-proj"
        slug = handoff.slug(cwd)
        (brief_dir / f"{slug}.md").write_text(
            "## Goal\nem dash — smart quote \u201cquoted\u201d arrow \u2192 done\n", encoding="utf-8")
        env = dict(os.environ, USERPROFILE=store, HOME=store)
        # handoff resolves STORE from the home directory at import time, so point
        # the child's home at the temp store.
        (Path(store) / ".claude" / "handoff").mkdir(parents=True, exist_ok=True)
        (Path(store) / ".claude" / "handoff" / f"{slug}.md").write_text(
            "## Goal\nem dash — smart quote \u201cquoted\u201d arrow \u2192 done\n", encoding="utf-8")
        result = subprocess.run([sys.executable, script, "inject"],
                                input=json.dumps({"cwd": cwd}), capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=60)
        check("inject exits 0", result.returncode == 0)
        check("em dash survives intact, not mangled", "—" in result.stdout,
              f"stdout was {result.stdout[:80]!r}")
        check("brief body is not lost", "smart quote" in result.stdout,
              f"stdout was {result.stdout[:120]!r}")


def main() -> int:
    print("handoff hook tests")
    saved = (handoff.STORE, handoff.CONFIG_PATH)
    try:
        test_allowlist()
        test_denied_project_never_calls_network()
        test_user_turn_filter()
        test_local_brief()
        test_git_branch_in_brief()
        test_malformed_transcripts()
        test_digest_keeps_opening_request()
        test_echo_guard()
        test_inject_freshness()
        test_exit_codes()
        test_unicode_survives_the_pipe()
    finally:
        handoff.STORE, handoff.CONFIG_PATH = saved

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
