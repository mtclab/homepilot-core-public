"""Whether THIS install's hub serves TLS, decided once and remembered.

ADR-004 S3 turned the hub's TLS on by default so a fresh install needs no
operator decision. Applying that default to an install that already has a fleet
is a different act entirely: the agents enrolled before the upgrade dial
plaintext, so flipping the listener strands every one of them at `EOF`, and
recovery means rewriting `/etc/homepilot/agent.env` on every managed host -
through the channel that just went down (#468).

So the default is a property of the INSTALL, not of the release:

* a new install gets TLS, with a generated certificate and nothing to configure;
* an install that already had enrolled agents when it first met this code keeps
  the transport its fleet is already speaking, and says so loudly;
* an operator who set ``HP_AGENT_HUB_TLS`` themselves is obeyed either way, and
  no decision is recorded on their behalf.

The decision is written to the settings table the first time it is taken and
never recomputed. Deciding per boot would mean the transport could flip later -
when the last legacy agent is removed, say - which is exactly the surprise this
exists to prevent.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SETTING_KEY = "hub_tls_mode"

MODE_TLS = "tls"
MODE_LEGACY_PLAINTEXT = "legacy_plaintext"

_LEGACY_NOTICE = (
    "This install had %d agent(s) enrolled before TLS-by-default, so the hub "
    "keeps serving PLAINTEXT on %s to avoid stranding them - their credentials "
    "and command traffic stay readable on the path. This is the transport the "
    "fleet was already using; nothing was downgraded. To move to TLS, migrate "
    "the agents (they need HP_AGENT_TLS=1 and the hub's fingerprint) and then "
    "set HP_AGENT_HUB_TLS=1."
)


_UNREADABLE_NOTICE = (
    "Could not read how many agents this install has enrolled, so the hub keeps "
    "serving PLAINTEXT on %s for this boot and NOTHING has been recorded. TLS by "
    "default is a decision taken once and kept, and taking it from a read that "
    "did not happen would strand a plaintext fleet with no way back. Fix the "
    "database read (or set HP_AGENT_HUB_TLS explicitly) and restart."
)


async def resolve_hub_tls_mode(repo: Any, *, tls_set_explicitly: bool, bind: str = "") -> str:
    """Return the TLS mode for this install, taking the decision once.

    ``repo`` is the Repository; ``tls_set_explicitly`` says whether the operator
    named ``HP_AGENT_HUB_TLS`` themselves, in which case their setting stands and
    nothing is persisted - a recorded decision would otherwise outlive the env
    var and quietly contradict it later.
    """
    if tls_set_explicitly:
        return MODE_TLS

    stored = await repo.get_setting(SETTING_KEY)
    if stored is not None and stored.get("value") in (MODE_TLS, MODE_LEGACY_PLAINTEXT):
        return str(stored["value"])

    enrolled = await _enrolled_agent_count(repo)
    if enrolled is None:
        # "I could not look" is not "there is no fleet" (#642). Taking the
        # TLS-by-default decision here would PERSIST it forever off a read that
        # never happened, and the install it strands is precisely the one this
        # module exists to protect: a plaintext fleet whose only repair channel
        # is the transport that just flipped. So this boot keeps the transport a
        # pre-existing fleet would be speaking, nothing is written, and the next
        # boot decides again once the count can be read.
        logger.warning(_UNREADABLE_NOTICE, bind or "the hub port")
        return MODE_LEGACY_PLAINTEXT

    mode = MODE_LEGACY_PLAINTEXT if enrolled else MODE_TLS
    await repo.set_setting(SETTING_KEY, mode)

    if mode == MODE_LEGACY_PLAINTEXT:
        logger.warning(_LEGACY_NOTICE, enrolled, bind or "the hub port")
    else:
        logger.info("Agent hub TLS enabled by default (no fleet predates it on this install)")
    return mode


async def _enrolled_agent_count(repo: Any) -> int | None:
    """How many agents this install has ever enrolled, or ``None`` if unreadable.

    Counts ROWS, not live connections: an agent that is merely switched off at
    upgrade time is still part of the fleet and will dial back in expecting the
    transport it enrolled with.

    ``None`` is the deliberate third answer. A failed or empty read used to come
    back as ``0``, which reads as "new install" and is the single most damaging
    wrong answer available here, so the two cases are kept apart.
    """
    try:
        row = await repo.db.fetchone("SELECT COUNT(*) c FROM agents")
    except Exception:
        logger.warning("Could not count enrolled agents", exc_info=True)
        return None
    if not row:
        return None
    try:
        return int(row["c"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Enrolled-agent count came back in an unusable shape: %r", row)
        return None
