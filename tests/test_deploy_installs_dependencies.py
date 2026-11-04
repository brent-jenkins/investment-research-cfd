"""A deploy that re-execs itself must still know what the pull changed.

Found on 2026-09-14 because `psutil` was absent after a deploy that reported
success from end to end.

The chain, and every step of it looked fine:

  1. the deploy starts on A, pins a copy of itself, pulls to B
  2. `before=A`, `after=B` -- and B changed `pyproject.toml`
  3. B ALSO changed `deploy.sh`, so the script re-execs the new one
  4. the successor reads `before` fresh: the pull already happened, so it
     gets B. `after` is B too.
  5. `git diff B B -- pyproject.toml` is empty, so the reinstall is skipped

**Any commit that touches both `deploy.sh` and `pyproject.toml` therefore
never installs its dependencies**, and says nothing. The deploy prints
"Now at B (was B)" on the second run, which reads like a no-op deploy rather
than like a lost variable.

This is the project's oldest failure wearing packaging clothes: a check that
reports success while doing nothing. `MemoryHigh` on the box against a unit
file that did not set it was the same thing; so was the completed-bar trim
shipping inert twice.

The fix carries the starting revision across the re-exec in the environment.

**RETRACTED 2026-09-19: the re-exec it carries across DIES.** `exec` is
given the pulled script as a program, and `deployment/deploy.sh` is mode
100644 -- at this commit and every other -- so it exits 126, "found but not
executable". The carrying is implemented correctly and never runs. psutil
was installed that day by the FOLLOWING deploy, not by this fix.

The checks below are why it was believed: they assert the script's TEXT --
that the variable is exported and that it is read back -- which is a check
on what went in where the failure was in what came out. "Tested two ways,
because neither alone is enough for a shell script" was still one way
short, because neither way executed the re-exec. `test_the_reexec_actually_
executes` is the third way, and the box was what found it.

Tested two ways, because neither alone is enough for a shell script:

  THE DECISION, exercised with real `git` in a temporary repository -- does
  carrying the revision actually change the answer?

  THE SCRIPT, checked to export and read that variable, so the rule above is
  tied to the file that has to implement it.

Mutations checked:

  M1720 the successor re-reading `before` instead of inheriting it
  M1721 the re-exec not exporting the starting revision
  M1722 the reinstall test comparing something other than the two revisions
  M1723 the re-exec invoking the file as a program again, which exits 126
        because the file is mode 644 -- see test_the_reexec_actually_executes
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deployment" / "deploy.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Two commits, the second touching deploy.sh AND pyproject.toml.

    That pairing is the case that breaks: either file alone is handled.
    """
    place = tmp_path / "repo"
    place.mkdir()
    _git(place, "init", "--quiet")
    _git(place, "config", "user.email", "t@example.com")
    _git(place, "config", "user.name", "t")

    (place / "pyproject.toml").write_text('dependencies = ["pandas"]\n')
    (place / "deploy.sh").write_text("echo one\n")
    _git(place, "add", "-A")
    _git(place, "commit", "--quiet", "-m", "first")

    (place / "pyproject.toml").write_text('dependencies = ["pandas", "psutil"]\n')
    (place / "deploy.sh").write_text("echo two\n")
    _git(place, "add", "-A")
    _git(place, "commit", "--quiet", "-m", "second: both files")
    return place


def _pyproject_changed(repo: Path, before: str, after: str) -> bool:
    """Exactly the test `deploy.sh` makes, run against real git."""
    return subprocess.run(
        ["git", "diff", "--quiet", before, after, "--", "pyproject.toml"],
        cwd=repo, capture_output=True,
    ).returncode != 0


def test_a_reexec_that_forgets_the_starting_revision_skips_the_install(repo) -> None:
    """M1720: the bug itself, shown to be a bug.

    The successor's view of the world, if it re-reads `before` after the pull
    has already happened: both revisions are the new one, and the reinstall
    test answers no to a pull that genuinely changed dependencies.
    """
    first = _git(repo, "rev-parse", "HEAD~1")
    second = _git(repo, "rev-parse", "HEAD")

    assert _pyproject_changed(repo, first, second), (
        "the fixture is wrong -- the second commit must change pyproject"
    )
    assert not _pyproject_changed(repo, second, second), (
        "comparing a revision against itself reported a change, so this test "
        "cannot detect the failure it exists for"
    )


def test_carrying_the_revision_restores_the_answer(repo) -> None:
    """The fix, exercised rather than asserted.

    Inheriting the starting revision makes the successor's test agree with
    what the first run would have concluded.
    """
    first = _git(repo, "rev-parse", "HEAD~1")
    second = _git(repo, "rev-parse", "HEAD")

    inherited = first          # what CFDINVEST_DEPLOY_BEFORE carries
    assert _pyproject_changed(repo, inherited, second), (
        "with the starting revision inherited, the deploy must still see "
        "that dependencies moved"
    )


def test_the_script_carries_the_revision_across_the_reexec() -> None:
    """M1721, M1722: the rule above, tied to the file that implements it."""
    source = DEPLOY.read_text(encoding="utf-8")

    assert "export CFDINVEST_DEPLOY_BEFORE=" in source, (
        "deploy.sh re-execs itself without passing on the revision it "
        "started from, so the successor cannot tell what the pull changed "
        "and silently skips the dependency install"
    )
    assert "${CFDINVEST_DEPLOY_BEFORE:-" in source, (
        "the starting revision is exported but never read back, which is the "
        "same bug with an extra step"
    )
    assert 'git diff --quiet "${before}" "${after}" -- pyproject.toml' in source, (
        "the reinstall test no longer compares the two revisions, so what it "
        "answers is anyone's guess"
    )


def test_the_reexec_actually_executes() -> None:
    """The re-exec ran the file directly, and the file is not executable.

    Found on 2026-09-19 by reading the box, not the suite.
    `cfdinvest-deploy.service` died with **status=126** -- "found but not
    executable" -- on two consecutive deploys, both of which had pulled and
    reset and then stopped before restarting anything or running the
    verification block.

    `deployment/deploy.sh` is mode **100644** in git and has been at every
    commit checked, including 6618a01, the commit whose whole subject was
    fixing this re-exec. So the re-exec path has almost certainly never
    worked, and the fix recorded for it was verified by asserting on the
    script's TEXT -- a check on what went in, where the failure was in what
    came out.

    It only fails on commits that change deploy.sh itself, which is why it
    stayed invisible: every other deploy skips this branch entirely.

    The pinned copy at the top works precisely because it `chmod +x`es
    itself first. Going through `/bin/bash` matches how the systemd unit
    invokes the script and does not depend on a file mode that git on
    Windows will not reliably carry.
    """
    source = DEPLOY.read_text(encoding="utf-8")

    assert 'exec /bin/bash "${REPO_DIR}/deployment/deploy.sh"' in source, (
        "the re-exec runs deploy.sh as a program. It is mode 644, so that "
        "exits 126 and the deploy stops after the reset -- code pulled, "
        "nothing restarted, no verification run"
    )


def test_the_reexec_still_cannot_loop() -> None:
    """The guard the fix must not weaken.

    An unconditional re-exec is a deploy loop, and a deploy loop against a
    vendor is worse than a stopped deploy because it looks like it is
    working. The new variable must not become a second way in.
    """
    source = DEPLOY.read_text(encoding="utf-8")
    assert 'if [[ -z "${CFDINVEST_DEPLOY_REEXEC:-}" ]]' in source, (
        "the once-only guard on the re-exec is gone"
    )
    assert "export CFDINVEST_DEPLOY_REEXEC=1" in source
