"""Plain words for every code the Overview can show (3.14).

The Overview's "What happened" printed journal codes as they were recorded -
``apply_refused``, ``guest_power_refused``, ``rollback_refused`` - and its
"Needs you" run lines said ``install_agent web-01 failed`` and ``is still
pending``. Each surface had its own partial table (the web's knew 50 journal
codes, the CLI none), so the same row read differently in the console, in
``hp status`` and over MCP.

This is the ONE table. The web Overview, ``hp status`` and the MCP
``get_audit_log`` / ``get_dashboard_summary`` tools all read the words the
server attaches here (``words`` on each journal row and each run line) and
show nothing else.

``tests/test_what_happened_wording.py`` derives every code the producers can
write - journal actions from every call that reaches ``Repository.log_audit``,
run actions and statuses from the ``tasks`` table's CHECK constraints - and
fails on any code without words here. A code recorded by an older version and
no longer produced is still said in words (:func:`_unknown`), never as the code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

On = Literal["change", "host", "none"]


@dataclass(frozen=True)
class Phrase:
    """One journal code, said as the end of a sentence that starts with who did it.

    ``verb`` then the thing acted on (``on``: the change's id, the host's name,
    or nothing), then ``tail``. ``many`` says several rows in a row by the same
    actor ("applied 4 changes"); ``{n}`` is the count. None: never grouped.
    """

    verb: str
    on: On = "none"
    tail: str = ""
    many: str | None = None


def _change(verb: str, many: str | None = None, tail: str = "") -> Phrase:
    return Phrase(verb, "change", tail, f"{verb} {{n}} {many}" if many else None)


def _host(verb: str, tail: str = "", many: str | None = None) -> Phrase:
    return Phrase(verb, "host", tail, f"{verb} {{n}} {many}" if many else None)


def _plain(verb: str, many: str | None = None) -> Phrase:
    return Phrase(verb, "none", "", many)


# Every journal action, by code. Keep the codes sorted within each group.
JOURNAL: dict[str, Phrase] = {
    # ── Changes (artifacts) ─────────────────────────────────────────────────
    "apply": _change("applied", "changes"),
    "apply_failed": _change("could not apply", "changes"),
    "apply_refused": _change("tried to apply", tail=" - stopped: the machine had changed"),
    "apply_unsettled": _change(
        "started applying", tail=" - the outcome is not known: Proxmox had not finished"
    ),
    "approve": _change("approved", "changes"),
    "edit": _change("edited", "changes"),
    "fail": Phrase("recorded as failed:", "change", "", "recorded {n} changes as failed"),
    "invalidate": _change("sent back to review", tail=": one of its steps was edited"),
    "propose": _change("proposed", "changes"),
    "recheck_refused": _change(
        "rechecked", tail=" and sent it back to review: the machine had changed"
    ),
    "reject": _change("rejected", "changes"),
    "replay": _change("ran again", "changes"),
    "replay_failed": _change("could not run again", "changes"),
    "replay_refused": _change("tried to run again", tail=" - stopped: the machine had changed"),
    "replay_unsettled": _change(
        "started running again", tail=" - the outcome is not known: Proxmox had not finished"
    ),
    "returned_for_review": _change("sent back to review", "changes"),
    "revoke": _change("revoked", "changes"),
    "rollback_refused": _change("tried to roll back", tail=" - stopped: the machine had changed"),
    "supersede": _change("replaced", "changes"),
    "unsettled": _change("left", tail=" waiting on Proxmox - the outcome is not known yet"),
    "withdraw": _change("withdrew", "approved changes"),
    # ── Machines ────────────────────────────────────────────────────────────
    "duplicate_host_retired": _host("retired a duplicate record of", many="duplicate records"),
    "guest_invite_minted": _host("made an invite for"),
    "guest_invite_revoked": _host("revoked an invite for"),
    "guest_power_refused": _host("tried a power action on", " - refused"),
    "guest_quota_removed": _host("removed the budget for"),
    "guest_quota_set": _host("set the budget for"),
    "guest_reboot": _host("rebooted"),
    "guest_shutdown": _host("shut down"),
    "guest_start": _host("started"),
    "guest_stop": _host("stopped", " (forced)"),
    "guest_template_cancelled": _host("cancelled building the guest template"),
    "guest_template_create_failed": _host("could not build the guest template"),
    "guest_template_created": _host("built the guest template"),
    "host_added": _host("added the host", many="hosts"),
    "host_forgotten": Phrase("forgot", "host", "", "forgot {n} machines"),
    "install_agent": _host("installed the agent on"),
    "install_agent_failed": _host("could not install the agent on"),
    "provision": _host("created the guest"),
    "provision_cancelled": _host("cancelled creating the guest"),
    "provision_failed": _host("could not create the guest"),
    "self_guest_cleared": _plain("cleared HomePilot's own host"),
    "self_guest_confirmed": _host("marked", " as HomePilot's own host"),
    "tailnet_join": _host("joined the tailnet:"),
    # ── The system's own work ───────────────────────────────────────────────
    "address_conflict": _plain("found two machines on one address"),
    "address_conflict_resolved": _plain("saw an address clash clear"),
    "check_cycle": _plain("checked every applied change for drift", "checked for drift {n} times"),
    "check_drift": Phrase("checked for drift:", "change", "", "checked {n} changes for drift"),
    "refresh": _plain(
        "read the inventory from Proxmox", "read the inventory from Proxmox {n} times"
    ),
    "refresh_incomplete": _plain("could not finish reading the inventory"),
    # ── Settings, access and the Overview ───────────────────────────────────
    "attention_snoozed": _plain(
        "put off a line on the Overview", "put off {n} lines on the Overview"
    ),
    "attention_unsnoozed": _plain("brought back a line on the Overview"),
    "instance_claimed": _plain("claimed this HomePilot"),
    "login": _plain("signed in"),
    "notification_channel_created": _plain("added a notification channel"),
    "notification_channel_deleted": _plain("removed a notification channel"),
    "notification_channel_disabled": _plain("turned off a notification channel"),
    "notification_channel_enabled": _plain("turned on a notification channel"),
    "notification_channel_tested": _plain("tested a notification channel"),
    # Phone alerts (#822).
    "phone_alerts_bundled_enabled": _plain("set up HomePilot's ntfy server for phone alerts"),
    "phone_alerts_bundled_disabled": _plain("stopped HomePilot's ntfy server for phone alerts"),
    "phone_alerts_public_url_set": _plain("set the address phones reach ntfy at"),
    "phone_alerts_phone_added": _plain("added a phone for alerts"),
    "phone_alerts_phone_allowed": _plain("let a phone read the alerts"),
    "phone_alerts_phone_removed": _plain("removed a phone from alerts"),
    "token_minted": _plain("made a token", "made {n} tokens"),
    "token_revoked": _plain("revoked a token", "revoked {n} tokens"),
    "webhook_created": _plain("added a webhook"),
    "webhook_deleted": _plain("removed a webhook"),
    "webhook_tested": _plain("tested a webhook"),
}

# What a run (a task) was doing, as the start of "... failed" / "... is still
# waiting to start". The target follows it.
RUN_ACTIONS: dict[str, str] = {
    "apply": "Applying",
    "create_guest_template": "Building the guest template",
    "install_agent": "Installing the agent on",
    "provision": "Creating the guest",
    "replay": "Running again",
    "revoke": "Revoking",
    "tailnet_join": "Joining the tailnet:",
    "withdraw": "Withdrawing",
}

# A run's state, as it ends "... is still <this>" (and in Records).
RUN_STATUSES: dict[str, str] = {
    "cancelled": "cancelled",
    "failed": "failed",
    "pending": "waiting to start",
    "running": "running",
    "succeeded": "done",
    "unknown": "of unknown outcome - Proxmox had not finished when the run stopped",
}


def _unknown(code: str) -> Phrase:
    """A code no current producer writes (an older version's), still in words."""
    spoken = code.replace("_", " ").replace("-", " ").strip() or "something"
    return Phrase(f"recorded: {spoken}")


def _details(entry: Mapping[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(entry.get("details_json") or "null")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def journal_words(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One journal row in words: ``{verb, object, tail, many, text}``.

    ``text`` is the whole fragment ("applied web-01's change") for surfaces that
    print a line; ``verb``/``object``/``tail`` are the same words in parts, for
    the console to set the object in bold. The actor is NOT included: each
    surface says who ("You", an assistant's label) its own way.
    """
    code = str(entry.get("action") or "")
    phrase = JOURNAL.get(code) or _unknown(code)
    verb, tail = phrase.verb, phrase.tail
    details = _details(entry)
    # Two codes say more when their details do.
    if code == "host_forgotten" and details.get("reason") == "destroyed by an applied change":
        tail = ", which an applied change destroyed"
    if code == "install_agent" and details.get("upgrade"):
        verb = "upgraded the agent on"
        if details.get("agent_version"):
            tail = f" to {details['agent_version']}"
    obj: str | None
    if phrase.on == "change":
        obj = str(entry.get("artifact_id") or "") or "a change"
    elif phrase.on == "host":
        obj = str(entry.get("target_host") or "") or "a host"
    else:
        obj = None
    text = verb + (f" {obj}" if obj else "") + tail
    return {"verb": verb, "object": obj, "tail": tail, "many": phrase.many, "text": text}


def with_journal_words(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Journal rows, each with its ``words``. What every surface returns."""
    return [{**row, "words": journal_words(row)} for row in rows]


def run_words(action: str, status: str, target: str) -> dict[str, str]:
    """A run line in words: ``what`` ("Installing the agent on web-01") and ``status``."""
    doing = RUN_ACTIONS.get(action) or f"Running {action.replace('_', ' ')}"
    return {
        "what": f"{doing} {target}".strip(),
        "status": RUN_STATUSES.get(status) or status.replace("_", " "),
    }


__all__ = [
    "JOURNAL",
    "RUN_ACTIONS",
    "RUN_STATUSES",
    "Phrase",
    "journal_words",
    "run_words",
    "with_journal_words",
]
