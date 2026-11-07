"""Make a public-mirror checkout match a validated, scrubbed export (#725).

The runbook used to say "copy the export over the mirror working tree, then
`git add -A`". Followed literally that does two wrong things:

  * it never DELETES anything. A file removed in the private repo - or newly
    added to the scrub's delete list - stays published on the mirror forever,
    because copying over a tree only adds and overwrites;
  * it overwrites what the MIRROR owns. The mirror carries its own workflows in
    `.github/` (the ones with real triggers, plus fixes made there: version
    stamping of the image, SHA256SUMS publishing that install-agent.sh relies
    on, failing on a failed dispatch). The private repo's copies were a stale
    fork of them, so every sync silently reverted those fixes.

This makes the mirror's tree EQUAL to the export, except for the paths the
mirror owns (MIRROR_OWNED), which it never touches. It refuses to run against a
mirror checkout with uncommitted changes, so everything it deletes or overwrites
is recoverable from git - and it never commits or pushes: the mirror commit is
the manual, reviewed step that follows.

Usage:
    python scripts/sync_export_to_mirror.py <export-dir> <mirror-checkout> [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Top-level paths the mirror owns and the export must never carry. `.git` is the
# mirror's history; `.github` is its CI and release plumbing (see module doc).
MIRROR_OWNED = (".git", ".github")


class SyncRefusedError(Exception):
    pass


def _git(mirror: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(mirror), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SyncRefusedError(f"git {' '.join(args)} failed in {mirror}: {proc.stderr.strip()}")
    return proc.stdout


def _owned(rel: Path) -> bool:
    return bool(rel.parts) and rel.parts[0] in MIRROR_OWNED


def _files(root: Path) -> set[Path]:
    """Every file and symlink under root, relative, skipping mirror-owned paths."""
    found: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        if rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if d not in MIRROR_OWNED]
        kept = []
        for d in dirnames:
            # A symlinked directory is an entry in its own right, not a tree to walk.
            if (here / d).is_symlink():
                found.add(rel_dir / d)
            else:
                kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            rel = rel_dir / name
            if not _owned(rel):
                found.add(rel)
    return found


def check(export: Path, mirror: Path) -> None:
    if not export.is_dir():
        raise SyncRefusedError(f"export is not a directory: {export}")
    if not mirror.is_dir():
        raise SyncRefusedError(f"mirror is not a directory: {mirror}")
    if export == mirror or export in mirror.parents or mirror in export.parents:
        raise SyncRefusedError("export and mirror must be separate, non-nested directories")
    for owned in MIRROR_OWNED:
        if (export / owned).exists():
            raise SyncRefusedError(
                f"the export carries {owned}/, which the mirror owns - re-run the "
                "scrub (it removes it) instead of syncing this tree"
            )
    if not (export / "PROVENANCE").is_file():
        raise SyncRefusedError(
            "the export has no PROVENANCE - it was not produced (and validated) by "
            "scrub-for-public.sh"
        )
    toplevel = Path(_git(mirror, "rev-parse", "--show-toplevel").strip()).resolve()
    if toplevel != mirror:
        raise SyncRefusedError(f"{mirror} is not the root of a git checkout (root: {toplevel})")
    if _git(mirror, "status", "--porcelain").strip():
        raise SyncRefusedError(
            "the mirror checkout has uncommitted changes; commit or discard them first "
            "so everything this sync deletes or overwrites stays recoverable from git"
        )


def plan(export: Path, mirror: Path) -> tuple[list[Path], list[Path]]:
    """(paths to delete from the mirror, paths to copy from the export)."""
    in_export = _files(export)
    in_mirror = _files(mirror)
    return sorted(in_mirror - in_export), sorted(in_export)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def apply(export: Path, mirror: Path, delete: list[Path], copy: list[Path]) -> None:
    for rel in delete:
        _remove(mirror / rel)
    for rel in copy:
        src, dest = export / rel, mirror / rel
        # A path that changed kind (file <-> dir <-> link) has to go first.
        if dest.is_symlink() or (dest.exists() and dest.is_dir() != src.is_dir()):
            _remove(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dest)
        else:
            shutil.copy2(src, dest)
    # Directories the deletions emptied, deepest first; never a mirror-owned one.
    for dirpath, _dirnames, _filenames in sorted(
        os.walk(mirror), key=lambda entry: len(Path(entry[0]).parts), reverse=True
    ):
        here = Path(dirpath)
        rel = here.relative_to(mirror)
        if rel == Path(".") or _owned(rel):
            continue
        if not any(here.iterdir()):
            here.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("export", type=Path)
    parser.add_argument("mirror", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = parser.parse_args(argv)
    export, mirror = args.export.resolve(), args.mirror.resolve()

    try:
        check(export, mirror)
    except SyncRefusedError as exc:
        print(f"sync refused: {exc}", file=sys.stderr)
        return 1

    delete, copy = plan(export, mirror)
    for rel in delete:
        print(f"delete  {rel}")
    if args.dry_run:
        print(f"(dry run) {len(delete)} to delete, {len(copy)} to copy; nothing changed")
        return 0
    apply(export, mirror, delete, copy)
    print(f"synced: {len(delete)} deleted, {len(copy)} copied; {', '.join(MIRROR_OWNED)} untouched")
    print("Review `git status` / `git diff` in the mirror, then commit as the scrub printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
