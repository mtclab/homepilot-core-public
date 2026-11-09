"""What a PVE task warning means, in one line, where we know (owner feedback 2026-11-08).

A PVE task that ends ``WARNINGS: n`` succeeded (#711), and the operator was shown
the count and nothing else - "WARNINGS: 1" on a backup that was fine reads like a
backup that was not. Each warning line is shown, verbatim, always; a recognised
one also carries a plain-words explanation and a verdict on whether it matters
for the task that logged it. An unknown one carries neither: saying "harmless"
about a warning nobody has read would be the same lie in a kinder voice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class KnownWarning:
    pattern: re.Pattern[str]
    explanation: str
    # True: the task did what it was asked; False: something the operator should
    # act on. A recognised warning always says which.
    harmless: bool


KNOWN_WARNINGS: tuple[KnownWarning, ...] = (
    KnownWarning(
        pattern=re.compile(r"EFI disk without 'ms-cert=2023k'", re.IGNORECASE),
        explanation=(
            "Harmless for this task: the VM's UEFI store does not yet hold "
            "Microsoft's 2023 Secure Boot certificates. Proxmox notes it on every "
            "start and backup; the task itself completed. It only matters for "
            "Secure Boot guests once the 2011 certificates expire - enrol the new "
            "ones from the VM's EFI disk when convenient."
        ),
        harmless=True,
    ),
)


def explain_warning(text: str) -> dict[str, Any]:
    """``{text, explanation, harmless}`` for one warning line.

    ``explanation`` and ``harmless`` are None when the warning is not one we
    recognise; the text is shown verbatim either way.
    """
    for known in KNOWN_WARNINGS:
        if known.pattern.search(text):
            return {"text": text, "explanation": known.explanation, "harmless": known.harmless}
    return {"text": text, "explanation": None, "harmless": None}
