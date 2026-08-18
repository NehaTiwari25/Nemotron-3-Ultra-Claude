"""
Hand one specified task to Nemotron and let it write the code.

Nemotron gets the handoff brief, the failing test, and the file to change, and
returns edits. This script applies them and runs the suite. The test is the gate,
so the answer is pass or fail rather than an opinion.

It retries, up to --attempts, and the retry is the part that earns its keep: each
failure goes back with what was tried, whether it applied, which checks it fixed,
and which still fail. A first patch that lands cleanly but fixes the wrong thing
is the common case, and it is recoverable only if the model is told what
happened. Two guards keep the loop honest - every attempt starts from the
ORIGINAL file, so a bad patch is replaced rather than compounded, and an
identical patch ends the loop instead of buying the same failure twice.

It is still not an agent: no shell, no file discovery, one file, one action.

Why bounded rather than autonomous:

  * A weak model with a shell is a bad trade. The failure mode of an agent loop
    is a repo full of plausible edits nobody asked for; the failure mode of this
    is a patch that does not apply, or applies and fails the tests. Both are
    cheap to throw away.
  * Requests are the scarce resource. An agent loop is 30-80 calls for one
    feature against a 50-a-day allowance. This is capped at --attempts, and
    stops early on success or on a repeated patch.
  * Edits are expressed as find/replace with a UNIQUE anchor, applied here. An
    ambiguous or missing anchor is caught locally instead of silently rewriting
    the wrong line.

    python delegate.py --task "..." --file handoff.py --test "python test_handoff.py"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import urllib.error
import urllib.request
from pathlib import Path

from handoff import STORE, api_key, load_config, project_allowed, record_work, slug

SYSTEM = """You are a senior engineer picking up someone else's work in progress.

You will be given: a handover brief describing the session so far, the current contents of one file, and the output of a failing test suite.

Make the failing tests pass. Change as little as possible. Match the surrounding style - naming, comment density, how errors are handled. Do not reformat untouched code, do not add dependencies, and do not "improve" anything the tests do not ask about.

Express every change as a find/replace pair. `find` MUST be copied byte-for-byte from the file shown to you and MUST appear EXACTLY ONCE in it; include surrounding lines if that is what makes it unique. This is applied mechanically, so an anchor that appears twice is discarded.

Your entire response must be the JSON object: it begins with { and ends with }, with no prose before or after it.

{
  "reasoning": "two or three sentences on what the tests require and how your change satisfies it",
  "edits": [
    { "find": "exact text from the file, unique", "replace": "the text that replaces it" }
  ]
}"""


def is_local(url: str) -> bool:
    return any(host in url for host in ("localhost", "127.0.0.1", "::1"))


def ask(system: str, user: str, *, max_tokens: int = 16000, timeout: int = 600,
        base_url: str | None = None, model: str | None = None) -> dict | None:
    """One request, returning the parsed object or None.

    `base_url` pointed at a local server is the answer for code that must not
    leave the machine. The endpoint speaks the same shape, so nothing else
    changes - and a local server needs no key, which is why the key check is
    skipped there rather than failing on a credential nobody needs.
    """
    config = dict(load_config())
    if base_url:
        config["api_url"] = base_url.rstrip("/") + "/chat/completions"
    if model:
        config["model"] = model

    key = "" if is_local(config["api_url"]) else api_key()
    if key is None:
        print("no OPENROUTER_API_KEY and no ../second-opinion/.env")
        return None

    def attempt(prefill: str) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if prefill:
            messages.append({"role": "assistant", "content": prefill})
        payload = json.dumps({
            "model": config["model"], "messages": messages,
            "temperature": 0.2, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            # Thinking ON here, unlike the brief: writing a patch against a test
            # is the one job in this project where deliberation earns its tokens.
            "reasoning": {"enabled": True},
        }).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "HTTP-Referer": "https://github.com/NehaTiwari25/Nemotron-3-Ultra-Claude",
                   "X-Title": "Claude handoff delegation"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(config["api_url"], data=payload, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        message = (body.get("choices") or [{}])[0].get("message", {})
        usage = body.get("usage") or {}
        print(f"  reply: {len(message.get('content') or '')} chars, "
              f"{usage.get('completion_tokens')} completion tokens "
              f"({(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')} reasoning)")
        return message.get("content") or ""

    for prefill in ("", "{"):
        try:
            raw = attempt(prefill)
        except (urllib.error.URLError, OSError, ValueError) as error:
            print(f"  request failed: {type(error).__name__}: {error}")
            return None
        for candidate in (raw.strip(), prefill + raw.strip()):
            first, last = candidate.find("{"), candidate.rfind("}")
            for text in ({candidate, candidate[first:last + 1]} if first != -1 else {candidate}):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and "edits" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    continue
        if not prefill:
            print("  unusable reply; retrying once prefilled to '{'")
    return None


def locate_loosely(text: str, find: str) -> tuple[int, int] | None:
    """Find `find` in `text` ignoring how much whitespace is between tokens.

    Smaller models reproduce code correctly and space it wrong: `s   =` where
    the file says `s =`. Byte-exact matching rejects those, and the model has no
    way to see its own error - it re-reads the file it was given and writes the
    same thing again. Three attempts, three identical rejections.

    Relaxing only whitespace RUNS keeps the safety that matters: the match must
    still be unique, and no amount of respacing turns one statement into a
    different one. Indentation inside the matched span is taken from the file,
    not from the model.
    """
    if not find.strip():
        return None
    pattern = r"\s+".join(re.escape(part) for part in find.split())
    matches = list(re.finditer(pattern, text))
    return (matches[0].start(), matches[0].end()) if len(matches) == 1 else None


def apply_edits(path: Path, edits: list[dict]) -> tuple[bool, list[str]]:
    """All-or-nothing. A half-applied patch is worse than none."""
    text = original = path.read_text(encoding="utf-8")
    notes = []
    for i, edit in enumerate(edits):
        find, replace = edit.get("find", ""), edit.get("replace", "")
        if not find:
            notes.append(f"edit {i}: empty anchor"); return False, notes

        count = text.count(find)
        if count == 1:
            text = text.replace(find, replace, 1)
            notes.append(f"edit {i}: applied ({len(find)} -> {len(replace)} chars)")
            continue
        if count > 1:
            notes.append(f"edit {i}: anchor appears {count} times, needs exactly 1")
            return False, notes

        span = locate_loosely(text, find)
        if span is None:
            notes.append(f"edit {i}: anchor appears 0 times, needs exactly 1")
            return False, notes
        start, end = span
        text = text[:start] + replace + text[end:]
        # Loud, because the anchor is not the only thing the model respaced. The
        # REPLACEMENT carries its spacing verbatim, and a model sloppy enough to
        # need this path writes `s   =  x` into the file. The tests will not
        # notice; a reviewer or a linter will.
        notes.append(f"edit {i}: applied on a whitespace-tolerant match "
                     f"({len(find)} -> {len(replace)} chars) - CHECK THE DIFF, "
                     f"the replacement's formatting comes from the model")

    if text == original:
        notes.append("patch changed nothing"); return False, notes
    path.write_text(text, encoding="utf-8")
    return True, notes


def failures(output: str) -> set[str]:
    """Names of failing checks, for telling a near miss from a patch that did nothing.

    Handles the two shapes in front of us - `FAILED: name` from the Python suite
    and `FAIL  name` from the Node one - because the retry feedback is much more
    useful when it can say which checks moved. A runner that reports differently
    yields an empty set, which costs precision and breaks nothing.
    """
    names = set()
    for line in output.splitlines():
        match = re.match(r"^\s*(FAILED|FAIL)\b[:\s]\s*(.+)$", line)
        if not match:
            continue
        # Trim each runner's trailing detail so the same check has one name:
        # pytest's " - AssertionError", the Node suite's " — got 2".
        name = re.split(r"\s+[-—]\s+", match.group(2).strip())[0]
        name = name.split("  ")[0].strip()
        if name:
            names.add(name)
    return names


def run(command: str, cwd: Path) -> tuple[bool, str]:
    """Run the gate, with cached bytecode routed somewhere harmless.

    Python invalidates a .pyc by size and mtime. A find/replace that swaps one
    operator for another - `a - b` for `a + b`, the archetypal minimal fix -
    leaves the size identical, and a retry loop rewrites the file within the same
    second. The result is a test run against the OLD bytecode: a correct patch
    reported as a failure, which then goes back to the model as "that did not
    work" and sends it looking for a bug that is not there.

    A fresh cache directory per run sidesteps it without deleting anything from
    the project. Harmless for non-Python test commands.
    """
    cache = Path(tempfile.mkdtemp(prefix="delegate-pycache-"))
    env = dict(os.environ, PYTHONPYCACHEPREFIX=str(cache), PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=900, env=env)
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--file", required=True, help="the one file Nemotron may change")
    parser.add_argument("--test", required=True, help="command that gates the patch")
    parser.add_argument("--base-url", default=None,
                        help="OpenAI-compatible endpoint; a localhost URL keeps "
                             "the code on this machine and skips the allowlist")
    parser.add_argument("--model", default=None, help="override the model id")
    parser.add_argument("--allow-unlisted", action="store_true",
                        help="send code from a project that is not allowlisted")
    parser.add_argument("--attempts", type=int, default=3,
                        help="how many tries before giving up; each costs one request")
    parser.add_argument("--project", default=str(Path.cwd()))
    parser.add_argument("--brief", default=None, help="handover brief; defaults to this project's")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    target = (project / args.file).resolve()
    if not target.exists():
        print(f"{target} not found")
        return 1

    # The same gate the hook uses. This path sends whole source files to a
    # third-party endpoint, so it needs the check MORE than the hook does, not
    # less - and it is invoked by hand, against whatever project you happen to be
    # standing in, which is exactly when the wrong one gets picked.
    going_offsite = not is_local(args.base_url or load_config()["api_url"])
    if going_offsite and not (project_allowed(str(project), load_config()) or args.allow_unlisted):
        print(f"{project} is not on the handoff allowlist, so its code will not be\n"
              f"sent anywhere. Add it to ~/.claude/handoff/config.json if it is\n"
              f"yours to share, or pass --allow-unlisted to override deliberately.\n\n"
              f"Client and bounty-scope code should not go through the free tier at\n"
              f"all: the endpoint collects session data.")
        return 1

    brief_file = Path(args.brief) if args.brief else STORE / f"{slug(str(project))}.md"
    brief = brief_file.read_text(encoding="utf-8") if brief_file.exists() else ""
    print(f"brief: {brief_file if brief else '(none found - proceeding without)'}")

    print(f"\nbaseline: {args.test}")
    passed, before = run(args.test, project)
    print("  " + (before.strip().splitlines() or ["(no output)"])[-1])
    if passed:
        print("\nThe suite already passes - there is nothing to delegate.")
        return 1

    artifacts = STORE / "delegate"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "tests-before.txt").write_text(before, encoding="utf-8")

    original = target.read_text(encoding="utf-8")
    (artifacts / f"{target.name}.original").write_text(original, encoding="utf-8")

    base_prompt = (
        (f"<handover_brief>\n{brief}\n</handover_brief>\n\n" if brief else "")
        + f"<file path=\"{args.file}\">\n{original}\n</file>\n\n"
        + f"<failing_tests>\n{before[-6000:]}\n</failing_tests>\n\n"
        + f"The task: {args.task}\n\n"
    )

    feedback = ""
    seen: set[str] = set()
    passed, requests, edits = False, 0, []

    for attempt in range(1, args.attempts + 1):
        # Every attempt starts from the ORIGINAL file. Stacking a second patch on
        # a first one that did not work compounds a mistake rather than replacing
        # it, and the model was shown the original - so that is what its anchors
        # are written against.
        target.write_text(original, encoding="utf-8")

        print(f"\n--- attempt {attempt} of {args.attempts} ---")
        prompt = base_prompt + feedback + (
            "Return the JSON object with your edits to the file above. Nothing else.")
        response = ask(SYSTEM, prompt, base_url=args.base_url, model=args.model)
        requests += 1
        if not response:
            print("  nothing usable came back; stopping")
            break

        edits = response.get("edits") or []
        if response.get("reasoning"):
            print(f"  reasoning: {str(response['reasoning'])[:220]}")
        print(f"  {len(edits)} edit(s) proposed")

        # An identical patch will fail identically. Spending another request to
        # watch that happen is the one thing a retry loop must not do.
        fingerprint = json.dumps(edits, sort_keys=True)
        if fingerprint in seen:
            print("  identical to a previous attempt; stopping rather than "
                  "spending another request on the same patch")
            break
        seen.add(fingerprint)

        (artifacts / f"response-{attempt}.json").write_text(
            json.dumps(response, indent=2), encoding="utf-8")

        applied, notes = apply_edits(target, edits)
        for note in notes:
            print(f"  {note}")

        if not applied:
            # A bad anchor is a different failure from a wrong fix, and the model
            # can only correct it if it is told which one happened.
            feedback = (
                f"<previous_attempt>\n{json.dumps(edits, indent=2)[:3000]}\n"
                f"</previous_attempt>\n\n<what_happened>\nThose edits could not be "
                f"applied: {'; '.join(notes)}. The `find` text must appear exactly "
                f"once in the file shown above, copied byte-for-byte. Choose "
                f"anchors that are unique.\n</what_happened>\n\n")
            continue

        (artifacts / f"{target.name}.patched-{attempt}").write_text(
            target.read_text(encoding="utf-8"), encoding="utf-8")

        print(f"  running: {args.test}")
        passed, after = run(args.test, project)
        (artifacts / f"tests-after-{attempt}.txt").write_text(after, encoding="utf-8")

        fixed = failures(before) - failures(after)
        remaining = failures(after)
        if fixed:
            print(f"  fixed ({len(fixed)}): " + "; ".join(sorted(fixed)))
        if remaining:
            print(f"  still failing ({len(remaining)}): " + "; ".join(sorted(remaining)))

        if passed:
            break

        feedback = (
            f"<previous_attempt>\n{json.dumps(edits, indent=2)[:3000]}\n"
            f"</previous_attempt>\n\n<what_happened>\nThat patch applied cleanly "
            f"but the suite still fails.\n"
            + (f"It did fix: {', '.join(sorted(fixed))}.\n" if fixed else
               "It fixed none of the failing checks.\n")
            + f"Still failing: {', '.join(sorted(remaining)) or 'see output'}.\n\n"
            f"Test output:\n{after[-3000:]}\n</what_happened>\n\n"
            "Write a different patch against the ORIGINAL file above - your "
            "previous edits were reverted, so do not assume they are present.\n\n")

    if not passed:
        target.write_text(original, encoding="utf-8")

    # Hand it back. The next session picks this up through the SessionStart hook,
    # so work done while Claude had no context is announced rather than
    # discovered later in a diff nobody expected.
    record_work(str(project), {
        "task": args.task, "file": args.file, "edits": len(edits),
        "passed": passed, "model": load_config()["model"], "test": args.test,
        "attempts": requests,
    })

    print(f"\n  artifacts: {artifacts}")
    print(f"  requests spent: {requests}")

    if passed:
        print(f"\nPASS on attempt {requests} - the suite is green. Left in place;\n"
              "review it with `git diff` before keeping it.\n"
              "Recorded for handback to the next session.")
        return 0

    print(f"\nFAIL after {requests} attempt(s). File restored to its original state.\n"
          "Recorded for handback anyway - a rejected attempt is worth knowing about.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
