"""
Carry work across a compaction boundary.

Claude Code compacts when the context fills, and what survives is a generic
summary. This writes a purpose-built continuation brief instead: what the
session was actually trying to do, what it already established, and what the
next step is - so the model on the other side of the boundary picks up where the
work was rather than where the summary left it.

Two hooks, two modes:

    PreCompact   -> `capture`   builds the brief and stores it
    SessionStart -> `inject`    prints it; Claude sees stdout as context

DESIGN RULES, each of which exists because the alternative is worse:

  * IT NEVER BLOCKS COMPACTION. Every path exits 0. A hook that fails, hangs, or
    raises would stall or break the session it was meant to help, so the whole
    body is wrapped and the API call has a hard timeout.

  * THE BRIEF IS BUILT LOCALLY FIRST, then optionally improved by Nemotron. The
    local part is free, instant, and always available - the API part runs on a
    50-requests-a-day free tier that is usually already spent on dataset
    generation. A design that only works when the quota is free is a design that
    usually does not work.

  * PROJECTS ARE OPT IN. The transcript contains everything the session touched:
    source, file contents, keys if any leaked into output. Sending that to a
    third-party endpoint is a decision to make per project, once, deliberately -
    not something a hook does automatically because it happened to fire. Unlisted
    projects never reach the network.

    That is not a general precaution. Client work and bounty-scope code must not
    go through the free tier at all, and this hook would otherwise do exactly
    that, silently, in the sessions where it matters most.

    python handoff.py capture   < hook json
    python handoff.py inject    < hook json
    python handoff.py test <transcript.jsonl>    # print a brief, no network
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
STORE = HOME / ".claude" / "handoff"
CONFIG_PATH = STORE / "config.json"

DEFAULT_CONFIG = {
    "allowed_projects": [],
    "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "api_url": "https://openrouter.ai/api/v1/chat/completions",
    "timeout_seconds": 75,
    "max_transcript_chars": 120_000,
    "brief_max_age_minutes": 180,
}

BRIEF_SYSTEM = """You are writing a handover note for an engineer taking over a coding session mid-task.

They have the repository and can read any file. What they do NOT have is the reasoning: what was tried, what failed and why, what was ruled out, which detail turned out to matter. That is what you supply.

Write these sections, in this order, and nothing else:

## Goal
One or two sentences: what this session is trying to achieve.

## State
What is actually done and verified, versus in progress. Be specific about evidence - a passing test, a measured number, a file that exists. If something is believed but unverified, say so.

## Next step
The single most useful thing to do next, concretely enough to start on.

## Hard-won details
The things that cost time and would cost it again: failures and their causes, settings that had to be a particular value, dead ends worth not re-entering. Omit anything obvious from reading the code.

Rules: no preamble, no praise, no restating the file tree. Prefer specifics over summary - "review call returned empty at 25k tokens, thinking was consuming the budget" beats "had some API issues". If the transcript does not support a section, write "nothing to report" under it rather than inventing content."""


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return config


def project_allowed(cwd: str, config: dict) -> bool:
    """Prefix match, case-insensitive: Windows paths vary in case between events.

    Default deny. An empty allowlist means nothing is sent anywhere, which is the
    correct behaviour for a tool whose failure mode is disclosure.
    """
    if not cwd:
        return False
    target = os.path.normcase(os.path.abspath(cwd))
    for allowed in config.get("allowed_projects", []):
        root = os.path.normcase(os.path.abspath(os.path.expanduser(allowed)))
        if target == root or target.startswith(root + os.sep):
            return True
    return False


def slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", os.path.abspath(cwd or "unknown")).strip("-").lower()[:80]


# --- transcript reading -----------------------------------------------------


def content_blocks(row: dict) -> list[dict]:
    message = row.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def read_transcript(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def texts_of(row: dict) -> list[str]:
    return [b.get("text", "") for b in content_blocks(row)
            if b.get("type") == "text" and b.get("text", "").strip()]


def is_real_user_turn(row: dict) -> bool:
    """A typed instruction, not a tool result or an injected reminder.

    Tool results arrive as `user` rows too, and system reminders are wrapped in
    tags. Counting those as instructions makes the brief think the user asked
    for things they never said.
    """
    if row.get("type") != "user" or row.get("isMeta"):
        return False
    blocks = content_blocks(row)
    if any(b.get("type") == "tool_result" for b in blocks):
        return False
    joined = " ".join(texts_of(row)).strip()
    if not joined or joined.startswith("<"):
        return False
    # "[Request interrupted by user]" and friends are the harness narrating, not
    # the user asking for something. Left in, they read as instructions and the
    # brief starts reporting requests that were never made.
    return not re.fullmatch(r"\[[^\]]{0,120}\]", joined)


def local_brief(rows: list[dict]) -> str:
    """The facts a machine can extract without help. Free, instant, always right."""
    instructions = [" ".join(texts_of(r)).strip() for r in rows if is_real_user_turn(r)]

    edited: list[str] = []
    commands: list[str] = []
    for row in rows:
        for block in content_blocks(row):
            if block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input") or {}
            if name in ("Edit", "Write", "NotebookEdit"):
                path = args.get("file_path")
                if path and path not in edited:
                    edited.append(path)
            elif name in ("Bash", "PowerShell"):
                description = args.get("description")
                if description:
                    commands.append(description)

    last_assistant = ""
    for row in reversed(rows):
        if row.get("type") == "assistant":
            texts = texts_of(row)
            if texts:
                last_assistant = texts[-1].strip()
                break

    parts = ["## Session facts", ""]
    if instructions:
        parts.append(f"**Opening request:** {instructions[0][:400]}")
        if len(instructions) > 1:
            parts.append("")
            parts.append("**Later instructions:**")
            for item in instructions[1:][-6:]:
                parts.append(f"- {item[:200]}")
    if edited:
        parts += ["", "**Files written or edited** (most recent last):"]
        parts += [f"- {p}" for p in edited[-15:]]
    if commands:
        parts += ["", "**Recent commands:**"]
        parts += [f"- {c}" for c in commands[-10:]]
    if last_assistant:
        parts += ["", "**Last thing said before the boundary:**", "",
                  last_assistant[:1200]]
    return "\n".join(parts)


def transcript_digest(rows: list[dict], limit: int) -> str:
    """A readable rendering of the tail, for the model to reason over.

    The tail rather than the whole thing: the end of a session is where the
    unfinished work is, and the opening request is prepended separately so the
    original goal never falls out of the window.
    """
    lines: list[str] = []
    for row in rows:
        kind = row.get("type")
        if kind == "assistant":
            for block in content_blocks(row):
                if block.get("type") == "text" and block.get("text", "").strip():
                    lines.append(f"ASSISTANT: {block['text'].strip()}")
                elif block.get("type") == "tool_use":
                    args = block.get("input") or {}
                    detail = args.get("description") or args.get("file_path") or ""
                    lines.append(f"  [tool] {block.get('name')} {str(detail)[:160]}")
        elif is_real_user_turn(row):
            lines.append(f"USER: {' '.join(texts_of(row)).strip()}")

    opening = next((line for line in lines if line.startswith("USER:")), "")
    tail = "\n".join(lines)
    if len(tail) > limit:
        tail = tail[-limit:]
        if opening:
            tail = f"{opening}\n\n[... middle of session omitted ...]\n\n{tail}"
    return tail


# --- the optional upgrade ---------------------------------------------------


def looks_like_echo(text: str) -> bool:
    """True when the reply is the transcript coming back rather than a note.

    The failure it catches: given a long transcript that stops mid-conversation,
    the model continues it instead of summarising it - replaying the session and
    answering its last message. Fencing the transcript and putting the ask last
    prevents it, and this is the check that notices when prevention fails.

    Keyed off the digest's own markers, which a real handover note has no reason
    to contain more than incidentally.
    """
    return text.count("  [tool] ") > 3 or text.count("ASSISTANT: ") > 2


def api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    # Reuse the key the MCP server already keeps rather than asking for a second.
    env_file = HOME / "second-opinion" / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def nemotron_brief(digest: str, config: dict) -> str | None:
    """One request. Returns None on anything at all going wrong."""
    key = api_key()
    if not key:
        return None

    payload = json.dumps({
        "model": config["model"],
        "messages": [
            {"role": "system", "content": BRIEF_SYSTEM},
            # The transcript is fenced, and the instruction comes AFTER it. With
            # the instruction only in the system prompt, a long transcript that
            # stops mid-conversation reads as something to continue: the first
            # version of this echoed the session back verbatim and carried on
            # answering the last message. Recency wins over the system prompt at
            # this length, so the ask goes last.
            {"role": "user", "content":
                "<transcript>\n" + digest + "\n</transcript>\n\n"
                "The transcript above is a coding session that is about to lose "
                "its context. Write the handover note for whoever picks the work "
                "up, using the four sections exactly as specified. Do not "
                "continue the session, do not reply to its last message, and do "
                "not quote the transcript back."},
            # No assistant prefill. Seeding the reply with "## Goal" looks like
            # it should help and instead ends the turn after ~70 tokens: the
            # model writes the first section, emits the next heading, and stops.
            # Fencing the transcript is what stops the echoing; the prefill was
            # belt-and-braces that cost three quarters of the note.
        ],
        "temperature": 0.2,
        "max_tokens": 1400,
        # Thinking off: the note IS the thinking, and a reasoning model left to
        # deliberate will spend the budget and return nothing. A hook cannot wait
        # five minutes regardless.
        "reasoning": {"enabled": False},
    }).encode("utf-8")

    request = urllib.request.Request(
        config["api_url"], data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/NehaTiwari25/Nemotron-3-Ultra-Claude",
            "X-Title": "Claude context handoff",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config["timeout_seconds"]) as response:
            body = json.loads(response.read().decode("utf-8"))
        text = ((body.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        return None if not text or looks_like_echo(text) else text
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        # Quota exhausted, offline, slow, malformed - all the same answer here.
        return None


# --- modes ------------------------------------------------------------------


def brief_path(cwd: str) -> Path:
    return STORE / f"{slug(cwd)}.md"


def capture(event: dict) -> None:
    config = load_config()
    cwd = event.get("cwd") or ""
    transcript = event.get("transcript_path") or ""
    if not transcript:
        return

    rows = read_transcript(transcript)
    if not rows:
        return

    sections = [
        f"<!-- handoff generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"session={event.get('session_id', '?')} -->",
        "# Continuation brief",
        "",
    ]

    if project_allowed(cwd, config):
        digest = transcript_digest(rows, config["max_transcript_chars"])
        written = nemotron_brief(digest, config)
        if written:
            sections += [written, "", "---", ""]
        else:
            sections += ["*(Nemotron unavailable - quota, network, or timeout. "
                         "Local facts only.)*", ""]
    else:
        sections += ["*(This project is not on the handoff allowlist, so nothing "
                     "was sent anywhere. Local facts only.)*", ""]

    sections.append(local_brief(rows))

    STORE.mkdir(parents=True, exist_ok=True)
    brief_path(cwd).write_text("\n".join(sections), encoding="utf-8")


def inject(event: dict) -> None:
    """Print the brief so Claude receives it as context. Silence if stale."""
    config = load_config()
    path = brief_path(event.get("cwd") or "")
    try:
        text = path.read_text(encoding="utf-8")
        age_minutes = (time.time() - path.stat().st_mtime) / 60
    except OSError:
        return

    if age_minutes > config["brief_max_age_minutes"]:
        return

    print("The previous context was compacted. This brief describes the work in "
          "progress at that point:\n")
    print(text)


def main() -> int:
    # Windows hands this a cp1252 stdout. The brief is full of em dashes and
    # smart quotes, and printing one raises UnicodeEncodeError - which the
    # catch-all below then swallows, so the hook exits 0 having delivered
    # nothing. Silent success is the one failure mode worth engineering against
    # here, because nobody ever notices it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "test":
        rows = read_transcript(sys.argv[2])
        print(f"[{len(rows)} rows]\n")
        print(local_brief(rows))
        print("\n--- digest (first 700 chars of what Nemotron would see) ---\n")
        print(transcript_digest(rows, load_config()["max_transcript_chars"])[:700])
        return 0

    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return 0

    try:
        if mode == "capture":
            capture(event)
        elif mode == "inject":
            inject(event)
    except Exception:
        # Deliberately broad. This runs inside someone's session; a traceback
        # here helps nobody and an exit code other than 0 can block compaction.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
