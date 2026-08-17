"""
Hand one specified task to Nemotron and let it write the code.

This is the experiment behind "can Nemotron take over and do the coding". It is
deliberately NOT an agent: no loop, no tool access, no file system. Nemotron gets
the handoff brief, the failing test, and the file to change, and returns edits.
This script applies them and runs the suite. The test is the gate, so the answer
is pass or fail rather than an opinion.

Why bounded rather than autonomous:

  * A weak model with a shell is a bad trade. The failure mode of an agent loop
    is a repo full of plausible edits nobody asked for; the failure mode of this
    is a patch that does not apply, or applies and fails the tests. Both are
    cheap to throw away.
  * Requests are the scarce resource. An agent loop is 30-80 calls for one
    feature against a 50-a-day allowance. This is one, occasionally two.
  * Edits are expressed as find/replace with a UNIQUE anchor, applied here. An
    ambiguous or missing anchor is caught locally instead of silently rewriting
    the wrong line.

    python delegate.py --task "..." --file handoff.py --test "python test_handoff.py"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from handoff import STORE, api_key, load_config, record_work, slug

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


def ask(system: str, user: str, *, max_tokens: int = 16000, timeout: int = 600) -> dict | None:
    """One request, returning the parsed object or None."""
    key = api_key()
    if not key:
        print("no OPENROUTER_API_KEY and no ../second-opinion/.env")
        return None

    config = load_config()

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
        request = urllib.request.Request(config["api_url"], data=payload, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/NehaTiwari25/Nemotron-3-Ultra-Claude",
            "X-Title": "Claude handoff delegation"})
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


def apply_edits(path: Path, edits: list[dict]) -> tuple[bool, list[str]]:
    """All-or-nothing. A half-applied patch is worse than none."""
    text = original = path.read_text(encoding="utf-8")
    notes = []
    for i, edit in enumerate(edits):
        find, replace = edit.get("find", ""), edit.get("replace", "")
        if not find:
            notes.append(f"edit {i}: empty anchor"); return False, notes
        count = text.count(find)
        if count != 1:
            notes.append(f"edit {i}: anchor appears {count} times, needs exactly 1")
            return False, notes
        text = text.replace(find, replace, 1)
        notes.append(f"edit {i}: applied ({len(find)} -> {len(replace)} chars)")
    if text == original:
        notes.append("patch changed nothing"); return False, notes
    path.write_text(text, encoding="utf-8")
    return True, notes


def run(command: str, cwd: Path) -> tuple[bool, str]:
    result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=900)
    return result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--file", required=True, help="the one file Nemotron may change")
    parser.add_argument("--test", required=True, help="command that gates the patch")
    parser.add_argument("--project", default=str(Path.cwd()))
    parser.add_argument("--brief", default=None, help="handover brief; defaults to this project's")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    target = (project / args.file).resolve()
    if not target.exists():
        print(f"{target} not found")
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

    prompt = (
        (f"<handover_brief>\n{brief}\n</handover_brief>\n\n" if brief else "")
        + f"<file path=\"{args.file}\">\n{target.read_text(encoding='utf-8')}\n</file>\n\n"
        + f"<failing_tests>\n{before[-6000:]}\n</failing_tests>\n\n"
        + f"The task: {args.task}\n\n"
        "Return the JSON object with your edits to the file above. Nothing else."
    )
    print(f"\nasking Nemotron ({len(prompt):,} chars of context)")
    response = ask(SYSTEM, prompt)
    if not response:
        print("\nNemotron produced nothing usable.")
        return 1

    if response.get("reasoning"):
        print(f"\n  its reasoning: {str(response['reasoning'])[:300]}")
    edits = response.get("edits") or []
    print(f"  {len(edits)} edit(s) proposed")

    # Keep the artifacts whatever happens. A rejected patch is the most
    # interesting output this script produces - reverting it and printing
    # "FAIL" throws away the only evidence of how close the model got.
    artifacts = STORE / "delegate"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "response.json").write_text(json.dumps(response, indent=2), encoding="utf-8")
    (artifacts / "tests-before.txt").write_text(before, encoding="utf-8")

    backup = target.read_text(encoding="utf-8")
    (artifacts / f"{target.name}.original").write_text(backup, encoding="utf-8")
    applied, notes = apply_edits(target, edits)
    if applied:
        (artifacts / f"{target.name}.patched").write_text(
            target.read_text(encoding="utf-8"), encoding="utf-8")
    for note in notes:
        print(f"  {note}")
    if not applied:
        target.write_text(backup, encoding="utf-8")
        print("\nPatch did not apply. File restored, nothing changed.")
        return 1

    print(f"\nre-running: {args.test}")
    passed, after = run(args.test, project)
    (artifacts / "tests-after.txt").write_text(after, encoding="utf-8")
    print("  " + (after.strip().splitlines() or ["(no output)"])[-1])

    # Partial credit is the useful signal: which checks it fixed, and which it
    # did not. "FAIL" alone cannot tell a near miss from a patch that did nothing.
    def failures(output: str) -> set[str]:
        return {line.split("FAILED:", 1)[1].strip()
                for line in output.splitlines() if "FAILED:" in line}

    fixed = failures(before) - failures(after)
    remaining = failures(after)
    if fixed:
        print(f"  fixed ({len(fixed)}): " + "; ".join(sorted(fixed)))
    if remaining:
        print(f"  still failing ({len(remaining)}): " + "; ".join(sorted(remaining)))
    print(f"  artifacts: {artifacts}")

    if not passed:
        target.write_text(backup, encoding="utf-8")

    # Hand it back. The next session picks this up through the SessionStart hook,
    # so work done while Claude had no context is announced rather than
    # discovered later in a diff nobody expected.
    record_work(str(project), {
        "task": args.task, "file": args.file, "edits": len(edits),
        "passed": passed, "model": load_config()["model"], "test": args.test,
    })

    if passed:
        print("\nPASS - Nemotron's patch makes the suite green. Left in place;\n"
              "review it with `git diff` before keeping it.\n"
              "Recorded for handback to the next session.")
        return 0

    print("\nFAIL - patch applied but the suite still fails. File restored.\n"
          "Recorded for handback anyway - a rejected attempt is worth knowing about.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
