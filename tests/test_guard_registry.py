"""The guard registry and the tests must not drift apart.

`break_the_guard.py` proves that each guard is load-bearing, but it takes about
half an hour and it edits the source tree, so it is not something anyone runs
after a one-line change. These checks are the cheap half: they run in the
ordinary suite, touch nothing, and catch the ways a case stops being connected
to the thing it claims to prove.

Three of those ways have a history here:

* A case names a test that has since been renamed or deleted. pytest exits 4
  for an unresolvable id, which the harness read as "non-zero, so the guard
  broke its test" -- reported `ok`, with nothing having run. The harness now
  classifies that as NOTFOUND, but classifying it still requires the half-hour
  run to notice. `test_every_case_names_a_test_that_exists` notices in a second.

* A test stops being the one that proves a guard -- rewritten to assert
  something adjacent, or split in two -- while the case still names it. Nothing
  mechanical can detect that in general. The `Guard: <ID>` line is the weakest
  form that does help: it puts the claim in front of whoever edits the test, and
  it fails here if they delete the test without deleting the case.

* A guard is registered with no break case at all. That is legitimate -- see
  `UNPROVABLE` -- but "no case" must be a claim someone made on purpose, not an
  absence. So an UNPROVABLE id has to be cited by a test as well.

The cross-reference is deliberately checked in BOTH directions. A one-way check
lets the unreferenced side rot: registry-to-test alone permits a `Guard:` line
naming an id that no longer exists, and test-to-registry alone permits a case
whose test forgot to cite it.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import break_the_guard as harness  # noqa: E402  -- after the path insert

# Matches `Guard: AUTH-001.` and `Guard: AUTH-001, AUTH-002.`
GUARD_LINE = re.compile(r"Guard:\s*((?:[A-Z]+-\d{3})(?:\s*,\s*[A-Z]+-\d{3})*)\s*\.")
GUARD_ID = re.compile(r"[A-Z]+-\d{3}")

REGISTERED = {c[0] for c in harness.CASES} | set(harness.UNPROVABLE)


def _functions(path: pathlib.Path) -> dict[str, str]:
    """Map every function in `path` to its own source text.

    Per function, not per file: a `Guard:` line anywhere in a 2,000-line test
    module would otherwise satisfy a case naming any test in it, which is not
    the property wanted. The claim is that *this test* proves *this guard*.
    """
    text = path.read_text()
    tree = ast.parse(text)
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = ast.get_source_segment(text, node) or ""
    return out


def _test_files() -> list[pathlib.Path]:
    return sorted((ROOT / "tests").glob("test_*.py"))


def test_every_registered_id_is_unique():
    """Two guards with one id would let either stand in for the other."""
    ids = [c[0] for c in harness.CASES] + list(harness.UNPROVABLE)
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate guard ids: {sorted(duplicates)}"


def test_every_case_names_a_test_that_exists():
    """A case whose test has been renamed proves nothing, silently.

    This is the cheap standin for the harness's NOTFOUND classification. It is
    an existence check by AST, not a pytest collection, so it costs milliseconds
    and cannot be confused by a database being unavailable.
    """
    missing = []
    for gid, label, _rel, _old, _new, test in harness.CASES:
        relpath, _, name = test.partition("::")
        path = ROOT / relpath
        if not path.exists():
            missing.append(f"{gid} ({label}): no such file {relpath}")
        elif name not in _functions(path):
            missing.append(f"{gid} ({label}): {relpath} has no test named {name}")
    assert not missing, "cases naming a test that does not exist:\n  " + "\n  ".join(missing)


def test_every_case_anchor_still_matches_exactly_once():
    """A case whose `old` string has moved cannot break anything.

    The harness needs to find `old` exactly once to remove it. Zero matches and
    it removes nothing, so the test passes and the case is reported ERROR -- but
    only during the half-hour run, which is not what anyone does after editing a
    function. More than one match and it removes several things at once, so the
    failure it observes need not come from the guard it names.

    This is the drift that actually happens here, and it happens through
    ordinary feature work rather than through anyone touching a guard: adding an
    argument to a signature, reindenting a block, renaming a local. The file
    being edited is a source file, the case lives in the tests, and nothing
    connects them at edit time. Checking it costs a string count.
    """
    broken = []
    for gid, label, relpath, old, _new, _test in harness.CASES:
        found = (ROOT / relpath).read_text().count(old)
        if found != 1:
            broken.append(f"{gid} ({label}): anchor matches {found}x in {relpath}")
    assert not broken, (
        "break cases whose anchor no longer matches exactly once:\n  "
        + "\n  ".join(broken))


def test_every_case_is_cited_by_the_test_it_names():
    """Registry -> test."""
    uncited = []
    for gid, label, _rel, _old, _new, test in harness.CASES:
        relpath, _, name = test.partition("::")
        source = _functions(ROOT / relpath).get(name, "")
        cited = {i for m in GUARD_LINE.finditer(source) for i in GUARD_ID.findall(m.group(1))}
        if gid not in cited:
            uncited.append(f"{gid} ({label}): {test} has no `Guard: {gid}.` line")
    assert not uncited, (
        "guards whose test does not cite them:\n  " + "\n  ".join(uncited))


def test_every_unprovable_guard_is_cited_by_some_test():
    """A guard with no break case still has to be someone's claim."""
    cited = set()
    for path in _test_files():
        for m in GUARD_LINE.finditer(path.read_text()):
            cited.update(GUARD_ID.findall(m.group(1)))
    orphans = sorted(set(harness.UNPROVABLE) - cited)
    assert not orphans, (
        f"registered as unprovable but cited by no test: {orphans}. An entry in "
        "UNPROVABLE asserts a guard exists; if nothing references it, nothing "
        "shows it does.")


def test_every_guard_line_in_the_tests_is_registered():
    """Test -> registry."""
    unknown = []
    for path in _test_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            for m in GUARD_LINE.finditer(line):
                for gid in GUARD_ID.findall(m.group(1)):
                    if gid not in REGISTERED:
                        unknown.append(f"{path.name}:{i}: {gid}")
    assert not unknown, (
        "`Guard:` lines naming an id with no registry entry:\n  "
        + "\n  ".join(unknown))


def _probe(tmp_path, body: str) -> pathlib.Path:
    path = tmp_path / "test_probe.py"
    path.write_text("import pytest\n\n" + body)
    return path


def test_a_missing_test_is_notfound_not_a_proof(tmp_path):
    """The silent pass this change exists to close.

    pytest exits 4 for an id it cannot resolve. The old rule was
    `returncode == 0`, so a non-zero exit meant "the guard broke its test", and
    a case naming a deleted or renamed test was printed as `ok`.
    """
    probe = _probe(tmp_path, "def test_real():\n    assert True\n")
    assert harness.run(f"{probe}::test_gone") == harness.NOTFOUND


def test_a_skipped_test_proves_nothing(tmp_path):
    probe = _probe(tmp_path, "def test_x():\n    pytest.skip('unavailable')\n")
    assert harness.run(f"{probe}::test_x") == harness.SKIPPED


def test_one_failing_parametrisation_is_a_proof(tmp_path):
    """Mixed parametrisations must resolve to FAILED, not PASSED.

    Written because the first version of `run()` ranked PASSED above FAILED and
    so read four load-bearing guards as unproven -- AUTH-030 among them, where
    5 of 8 parameters fail without the guard while 3 still pass. A guard whose
    removal turns any parametrisation red is load-bearing; the parameters that
    survive alongside it are not evidence to the contrary.
    """
    probe = _probe(tmp_path,
                   "@pytest.mark.parametrize('n', [1, 2, 3])\n"
                   "def test_x(n):\n"
                   "    assert n > 1\n")
    assert harness.run(f"{probe}::test_x") == harness.FAILED


@pytest.mark.parametrize("gid,why", sorted(harness.UNPROVABLE.items()))
def test_unprovable_entries_state_a_reason(gid, why):
    """The reason is the entire value of the entry.

    Without it, UNPROVABLE is a list of guards excused from proof for reasons
    nobody recorded, which is a worse position than not registering them.
    """
    assert len(why.strip()) > 40, f"{gid}: reason is too short to be one"
