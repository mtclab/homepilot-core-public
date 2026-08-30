"""The alert rules a fresh install starts with (ADR-004 corollary 2).

ADR-004 S5 claims "Nothing installs, imports or configures; monitoring is part
of the product." Collection was; alerting was not. A fresh install stored
metrics from the first minute and had **zero** alert rules, so the Overview's
``firing_alerts: 0`` meant "nothing is being watched" while reading as "all
well" - and the operator had to know, unprompted, that alert rules exist, where
they live and what to set them to. That is precisely the by-hand step the ADR
forbids:

    Every new feature is now judged by "what must an operator do by hand for
    this to work?", where the acceptable answer is "nothing".

Corollary 2 says what to do instead: *"A capability that needs an operator
decision must pick a safe default and proceed, not refuse to start."*

## Why these two, and why they are floors rather than percentages

Both are ABSOLUTE floors, and that is the whole design. A percentage default
("90% full") would need a ratio metric the agent does not emit, and would be
wrong in opposite directions on a 20 GB VM and a 4 TB array. A floor cannot be
wrong: under a gigabyte free on ``/`` breaks package installs, logging and
journald on any Linux host, and under 200 MB available memory means the OOM
killer is the next thing to happen. The cost of a floor is that it is LATE on a
big host, never that it is noisy on a small one - the correct bias for something
nobody asked for.

Ten minutes of duration, so a build that briefly fills ``/tmp`` does not page
anyone.

They are a starting point, not a monitoring strategy, and an operator is meant
to retune or delete them. Which is why:

## Seeded exactly once, and never onto an existing policy

Two guards, and both are needed:

* a marker in ``settings``, so deleting a default does not resurrect it on the
  next restart - a rule an operator removed staying removed matters more than a
  default existing;
* and only when the install has NO alert rules at all. An upgrade must not add
  rules to a fleet that already has an alerting policy: an operator with five
  tuned rules did not ask for two more, and their absence is not the failure
  this exists to fix. An install with rules that are all inert is a different
  problem, and ``hosts_matched`` is what says so.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Bump the suffix only to deliberately re-seed an existing fleet; existing
# installs then get the new set once, and never again.
SEED_MARKER_KEY = "default_alert_rules_seeded_v1"

DEFAULT_ALERT_RULES: tuple[dict[str, Any], ...] = (
    {
        "name": "Disk almost full",
        "metric": "disk.free_gb",
        "comparison": "lt",
        "threshold": 1.0,
        "for_seconds": 600,
        "host_filter": "*",
    },
    {
        "name": "Memory almost exhausted",
        "metric": "memory.free_gb",
        "comparison": "lt",
        "threshold": 0.2,
        "for_seconds": 600,
        "host_filter": "*",
    },
)


async def seed_default_alert_rules(repo: Any, metrics_repo: Any) -> int:
    """Create the default rules once. Returns how many were created.

    Never raises into startup: a fleet that cannot write a default rule must
    still boot. It logs, and the next boot tries again because the marker is
    only written after the rules are."""
    try:
        if await repo.get_setting(SEED_MARKER_KEY) is not None:
            return 0
        if await metrics_repo.list_rules():
            # An existing alerting policy, seeded before this ever ran. Mark it
            # done so the question is not asked again, and change nothing.
            await repo.set_setting(SEED_MARKER_KEY, "1")
            return 0
        created = 0
        for spec in DEFAULT_ALERT_RULES:
            await metrics_repo.create_rule(**spec)
            created += 1
        await repo.set_setting(SEED_MARKER_KEY, "1")
        logger.info(
            "seeded %d default alert rule(s) so a fresh install watches something "
            "(ADR-004 corollary 2); retune or delete them in Settings -> Monitoring",
            created,
        )
        return created
    except Exception:
        logger.warning("could not seed the default alert rules", exc_info=True)
        return 0
