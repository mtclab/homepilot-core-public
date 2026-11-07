"""The mirror-aware file list in tests/tree.py cannot drift from the scrub (gate).

A test that reads an overlay file requires it in the private tree and expects it
gone in the public mirror. That only holds while MIRROR_DROPPED names exactly
what scripts/scrub-for-public.sh deletes, and while each tree really is what
tests/tree.py says it is.
"""

import re

import pytest

from tests.tree import IS_PUBLIC_MIRROR, MIRROR_DROPPED, REPO_ROOT


@pytest.mark.skipif(IS_PUBLIC_MIRROR, reason="the mirror does not ship the scrub script")
def test_every_file_listed_as_dropped_is_deleted_by_the_scrub() -> None:
    scrub = (REPO_ROOT / "scripts" / "scrub-for-public.sh").read_text(encoding="utf-8")
    deleted: set[str] = set()
    for args in re.findall(r"^\s*rm -r?f (.+?)\s*$", scrub, flags=re.MULTILINE):
        deleted.update(p.rstrip("/") for p in args.split())
    not_deleted = sorted(MIRROR_DROPPED - deleted)
    assert not not_deleted, (
        f"tests/tree.py treats {not_deleted} as absent from the mirror, "
        "but scripts/scrub-for-public.sh does not delete them"
    )


def test_the_overlay_is_present_in_the_private_tree_and_gone_in_the_mirror() -> None:
    present = sorted(name for name in MIRROR_DROPPED if (REPO_ROOT / name).exists())
    if IS_PUBLIC_MIRROR:
        assert not present, f"the mirror still ships {present}, which the scrub removes"
    else:
        assert present == sorted(MIRROR_DROPPED), (
            f"the private tree is missing {sorted(MIRROR_DROPPED - set(present))}"
        )
