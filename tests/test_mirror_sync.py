"""Syncing an export to the public mirror deletes what was removed and never
touches what the mirror owns (#725).

The runbook said "copy the export over the mirror tree, then `git add -A`". A
copy-over never deletes: a file removed privately, or newly put on the scrub's
delete list, stayed published. And the export carried the private repo's stale
copies of the release workflows, which overwrote the mirror's - reverting the
fixes made only there.

Teeth: replace the sync with a plain copy-over and
`test_a_file_removed_privately_leaves_the_mirror` fails; drop `.github` from
MIRROR_OWNED and the workflow test fails; drop `rm -rf .github/` from the scrub
and `test_the_scrub_never_exports_workflows` fails.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC = REPO_ROOT / "scripts" / "sync_export_to_mirror.py"
MIRROR_WORKFLOW = "on:\n  push:\n    tags: ['v*']\n# the mirror's own release, with fixes\n"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    root = tmp_path / "mirror"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, ".github/workflows/release.yml", MIRROR_WORKFLOW)
    _write(root, "src/kept.py", "OLD = 1\n")
    _write(root, "docs/removed-privately.md", "an internal note that must not stay public\n")
    _write(root, "PROVENANCE", "source_commit=old\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "previous sync")
    return root


@pytest.fixture
def export(tmp_path: Path) -> Path:
    root = tmp_path / "export"
    _write(root, "src/kept.py", "NEW = 2\n")
    _write(root, "src/added.py", "ADDED = 3\n")
    _write(root, "PROVENANCE", "source_commit=new\n")
    return root


def _sync(export: Path, mirror: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SYNC), str(export), str(mirror), *extra],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_file_removed_privately_leaves_the_mirror(export: Path, mirror: Path) -> None:
    proc = _sync(export, mirror)
    assert proc.returncode == 0, proc.stderr
    assert not (mirror / "docs" / "removed-privately.md").exists()
    assert not (mirror / "docs").exists(), "an emptied directory was left behind"
    assert (mirror / "src" / "kept.py").read_text() == "NEW = 2\n"
    assert (mirror / "src" / "added.py").read_text() == "ADDED = 3\n"
    assert (mirror / "PROVENANCE").read_text() == "source_commit=new\n"


def test_the_mirror_keeps_its_own_workflows_and_history(export: Path, mirror: Path) -> None:
    proc = _sync(export, mirror)
    assert proc.returncode == 0, proc.stderr
    assert (mirror / ".github/workflows/release.yml").read_text() == MIRROR_WORKFLOW
    assert _git(mirror, "log", "--oneline").strip(), "the mirror's history was touched"


def test_an_export_carrying_workflows_is_refused(export: Path, mirror: Path) -> None:
    _write(export, ".github/workflows/release.yml", "on:\n  workflow_dispatch:\n# stale fork\n")
    proc = _sync(export, mirror)
    assert proc.returncode != 0
    assert (mirror / ".github/workflows/release.yml").read_text() == MIRROR_WORKFLOW


def test_a_dirty_mirror_is_refused_and_untouched(export: Path, mirror: Path) -> None:
    _write(mirror, "uncommitted.txt", "work in progress\n")
    proc = _sync(export, mirror)
    assert proc.returncode != 0
    assert "uncommitted" in proc.stderr
    assert (mirror / "docs" / "removed-privately.md").exists()


def test_an_unvalidated_export_is_refused(export: Path, mirror: Path) -> None:
    (export / "PROVENANCE").unlink()
    assert _sync(export, mirror).returncode != 0


def test_dry_run_names_the_deletions_and_changes_nothing(export: Path, mirror: Path) -> None:
    proc = _sync(export, mirror, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "delete  docs/removed-privately.md" in proc.stdout
    assert (mirror / "docs" / "removed-privately.md").exists()
    assert (mirror / "src" / "kept.py").read_text() == "OLD = 1\n"


@pytest.mark.skipif(
    not (REPO_ROOT / "scripts" / "scrub-for-public.sh").exists(),
    reason="the scrub tooling is private: it is removed from the public export",
)
def test_the_scrub_never_exports_workflows() -> None:
    scrub = (REPO_ROOT / "scripts" / "scrub-for-public.sh").read_text(encoding="utf-8")
    v2_case = scrub.split("    homepilot-v2)\n", 1)[1].split("\n        ;;\n", 1)[0]
    assert "rm -rf .github/" in v2_case
    validate = (REPO_ROOT / "scripts" / "validate-scrub.sh").read_text(encoding="utf-8")
    deleted_dirs = validate.split("DELETED_DIRS=(", 1)[1].split(")", 1)[0]
    assert '".github"' in deleted_dirs
    assert "sync_export_to_mirror.py" in scrub, "the printed runbook still says copy-over"
