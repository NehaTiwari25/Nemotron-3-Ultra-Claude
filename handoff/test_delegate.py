"""
Tests for the delegation loop. No network - the model is stubbed.

What is worth testing here is not "does it call the API". It is whether the loop
spends requests wisely and leaves the repository in a defensible state when the
model is wrong, which is the normal case.

    python test_delegate.py
"""

from __future__ import annotations

import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import delegate

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  {'pass' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not condition else ""))


def project(source: str = "def add(a, b):\n    return a - b\n") -> Path:
    """A tiny project whose test prints FAILED: in the shape the loop reads."""
    work = Path(tempfile.mkdtemp())
    (work / "mod.py").write_text(source, encoding="utf-8")
    (work / "t.py").write_text(
        "from mod import add\n"
        "ok = add(2, 3) == 5\n"
        "print('pass' if ok else 'FAILED: add is wrong')\n"
        "raise SystemExit(0 if ok else 1)\n", encoding="utf-8")
    return work


def drive(work: Path, replies: list[dict], attempts: int = 3) -> tuple[int, list[str]]:
    """Run main() against a scripted model. Returns exit code and the prompts sent."""
    prompts: list[str] = []
    remaining = list(replies)

    def fake_ask(system, prompt, **kwargs):
        prompts.append(prompt)
        return remaining.pop(0) if remaining else None

    original_ask, original_record = delegate.ask, delegate.record_work
    delegate.ask = fake_ask
    delegate.record_work = lambda *a, **k: None
    argv = sys.argv
    # Temp projects are not on the allowlist, which is the correct answer for
    # real use and would stop every test here. The guard gets its own test below.
    sys.argv = ["delegate.py", "--task", "make add work", "--file", "mod.py",
                "--test", f'"{sys.executable}" t.py', "--project", str(work),
                "--attempts", str(attempts), "--allow-unlisted"]
    try:
        with redirect_stdout(io.StringIO()):
            code = delegate.main()
    finally:
        delegate.ask, delegate.record_work = original_ask, original_record
        sys.argv = argv
    return code, prompts


def edit(find: str, replace: str) -> dict:
    return {"reasoning": "stub", "edits": [{"find": find, "replace": replace}]}


def test_apply_edits() -> None:
    print("\napplying a patch")
    path = Path(tempfile.mkdtemp()) / "f.py"

    path.write_text("alpha\nbeta\n", encoding="utf-8")
    ok, _ = delegate.apply_edits(path, [{"find": "alpha", "replace": "gamma"}])
    check("unique anchor applies", ok and "gamma" in path.read_text(encoding="utf-8"))

    path.write_text("dup\ndup\n", encoding="utf-8")
    ok, notes = delegate.apply_edits(path, [{"find": "dup", "replace": "x"}])
    check("ambiguous anchor refused", not ok and "2 times" in " ".join(notes))
    check("file untouched after refusal", path.read_text(encoding="utf-8") == "dup\ndup\n")

    path.write_text("alpha\n", encoding="utf-8")
    ok, _ = delegate.apply_edits(path, [{"find": "nope", "replace": "x"}])
    check("missing anchor refused", not ok)

    ok, notes = delegate.apply_edits(path, [{"find": "alpha", "replace": "alpha"}])
    check("no-op patch refused", not ok and "changed nothing" in " ".join(notes))

    # All-or-nothing: a good first edit must not survive a bad second one.
    path.write_text("one\ntwo\n", encoding="utf-8")
    ok, _ = delegate.apply_edits(path, [{"find": "one", "replace": "1"},
                                        {"find": "missing", "replace": "x"}])
    check("partial patch is not written", not ok and path.read_text(encoding="utf-8") == "one\ntwo\n")


def test_whitespace_tolerant_anchor() -> None:
    """Small models get the code right and the spacing wrong.

    Observed on the live run: `s   =` proposed against a file containing `s =`,
    three times, because a byte-exact rejection gives the model nothing to
    correct - it re-reads the same file and writes the same anchor again.
    """
    print("\nanchors survive respacing, but only when unique")
    path = Path(tempfile.mkdtemp()) / "f.py"

    path.write_text('    s = sub("_", (name or "").strip())\n    return s\n', encoding="utf-8")
    ok, notes = delegate.apply_edits(path, [
        {"find": '    s   =  sub("_",   (name or   "").strip())', "replace": "    s = 'fixed'"}])
    check("respaced anchor still applies", ok, "; ".join(notes))
    check("noted as a loose match", any("whitespace-tolerant" in n for n in notes))
    check("replacement written", "'fixed'" in path.read_text(encoding="utf-8"))

    # An EXACT unique match wins outright. The loose path is a fallback, so a
    # well-formed anchor keeps working in a file that happens to hold a respaced
    # near-twin - otherwise tightening the spacing elsewhere breaks good patches.
    path.write_text("x = 1\nx  =  1\n", encoding="utf-8")
    ok, notes = delegate.apply_edits(path, [{"find": "x = 1", "replace": "x = 2"}])
    check("exact unique match wins over a respaced twin", ok)
    check("it was the exact path, not the loose one",
          not any("whitespace-tolerant" in n for n in notes))
    check("only the exact line changed",
          path.read_text(encoding="utf-8") == "x = 2\nx  =  1\n")

    # Uniqueness still decides inside the fallback: no exact match, two loose
    # candidates, so nothing is written.
    before = "y  =  1\ny   =   1\n"
    path.write_text(before, encoding="utf-8")
    ok, _ = delegate.apply_edits(path, [{"find": "y = 1", "replace": "y = 2"}])
    check("ambiguous loose match is refused", not ok)
    check("file untouched when loosely ambiguous", path.read_text(encoding="utf-8") == before)

    # Relaxing whitespace must not let a different statement match.
    path.write_text("total = a + b\n", encoding="utf-8")
    ok, _ = delegate.apply_edits(path, [{"find": "total = a - b", "replace": "total = 0"}])
    check("different code still does not match", not ok)


def test_failure_parsing() -> None:
    print("\nreading the gate's output")
    check("names extracted", delegate.failures("FAILED: a\nok\nFAILED: b") == {"a", "b"})
    check("clean run yields nothing", delegate.failures("all good") == set())


def test_retry_succeeds_after_feedback() -> None:
    print("\nretry loop")
    work = project()
    code, prompts = drive(work, [edit("return a - b", "return a * b"),
                                 edit("return a - b", "return a + b")])
    check("succeeds on the second attempt", code == 0)
    check("spent exactly two requests", len(prompts) == 2, f"spent {len(prompts)}")
    check("fix is left in place", "a + b" in (work / "mod.py").read_text(encoding="utf-8"))
    check("second prompt carried the failure back", "still fails" in prompts[1])
    check("second prompt showed the previous edits", "return a * b" in prompts[1])


def test_same_size_edit_is_not_hidden_by_stale_bytecode() -> None:
    """A one-operator fix leaves the file the same size, within the same second.

    Python invalidates .pyc on size and mtime, so without a cache workaround the
    test runs against the OLD bytecode and a correct patch looks like a failure.
    """
    print("\nsame-size edit is actually seen by the test run")
    work = project()
    code, prompts = drive(work, [edit("return a - b", "return a * b"),
                                 edit("return a - b", "return a + b")])
    check("correct same-size patch is recognised as passing", code == 0,
          "stale bytecode hid the fix")


def test_bad_anchor_gets_specific_feedback() -> None:
    print("\nfeedback distinguishes a bad anchor from a wrong fix")
    work = project()
    _, prompts = drive(work, [edit("NOT IN FILE", "x"),
                              edit("return a - b", "return a + b")])
    check("told the patch could not be applied", "could not be applied" in prompts[1])
    check("told what the anchor rule is", "exactly once" in prompts[1])


def test_identical_patch_stops_the_loop() -> None:
    print("\nan identical patch ends the loop")
    work = project()
    same = edit("return a - b", "return a * b")
    code, prompts = drive(work, [same, same, same], attempts=3)
    check("stopped before the third request", len(prompts) == 2, f"spent {len(prompts)}")
    check("reported failure", code == 1)


def test_file_restored_when_every_attempt_fails() -> None:
    print("\nthe repository is left as it was found")
    work = project()
    before = (work / "mod.py").read_text(encoding="utf-8")
    code, _ = drive(work, [edit("return a - b", "return a * b"),
                           edit("return a - b", "return a / b")], attempts=2)
    check("exit code reports failure", code == 1)
    check("file restored exactly", (work / "mod.py").read_text(encoding="utf-8") == before)


def test_unlisted_project_sends_nothing() -> None:
    """The guard that matters most: this path ships whole source files.

    It is run by hand, against whatever project you are standing in, which is
    exactly the situation where the wrong one gets picked.
    """
    print("\nunlisted projects are refused before anything is sent")
    work = project()
    prompts = []
    original_ask, original_record = delegate.ask, delegate.record_work
    delegate.ask = lambda *a, **k: prompts.append(1)
    delegate.record_work = lambda *a, **k: None
    argv = sys.argv
    sys.argv = ["delegate.py", "--task", "t", "--file", "mod.py",
                "--test", f'"{sys.executable}" t.py', "--project", str(work)]
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            code = delegate.main()
    finally:
        delegate.ask, delegate.record_work = original_ask, original_record
        sys.argv = argv

    check("refuses to run", code == 1)
    check("no request made", not prompts)
    check("says why", "not on the handoff allowlist" in out.getvalue())
    check("names the override", "--allow-unlisted" in out.getvalue())
    check("warns about client code", "free tier" in out.getvalue())


def test_passing_suite_is_not_delegated() -> None:
    print("\nnothing to do is not a task")
    work = project(source="def add(a, b):\n    return a + b\n")
    code, prompts = drive(work, [edit("x", "y")])
    check("refuses to delegate", code == 1)
    check("spent no requests", len(prompts) == 0, f"spent {len(prompts)}")


def main() -> int:
    print("delegation loop tests")
    test_apply_edits()
    test_whitespace_tolerant_anchor()
    test_failure_parsing()
    test_retry_succeeds_after_feedback()
    test_same_size_edit_is_not_hidden_by_stale_bytecode()
    test_bad_anchor_gets_specific_feedback()
    test_identical_patch_stops_the_loop()
    test_file_restored_when_every_attempt_fails()
    test_unlisted_project_sends_nothing()
    test_passing_suite_is_not_delegated()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
