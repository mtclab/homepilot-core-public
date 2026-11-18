"""The shell a guest-disk change runs INSIDE the guest, as root, via the QEMU guest agent (#824).

One script, three modes, so the check before the attach, the work after it and
the drift check can never disagree about what "the disk", "blank" or "mounted
right" mean:

* ``preflight`` - read-only, before anything is attached: the tools exist, the
  mount point is free (absent, or an empty directory nobody mounts on), no
  fstab line and no other device already claims the mount point or the label;
* ``apply`` - find the disk by its serial (waiting up to 30 s for it to
  appear), refuse unless it is blank, mkfs with the label, create the mount
  point (empty, immutable), back up fstab, append ONE line, mount that one
  mount point (never ``mount -a``), verify with findmnt;
* ``verify`` - read-only facts for drift.

Every step prints ``HP-STEP <id> <done|skipped|failed> <words>``; ``verify``
prints ``HP-FACT <key> <value>``. Exit 0 = done, 3 = refused (this step acted on
nothing), 4 = a step that acts failed. POSIX sh: the guest may have only dash or
busybox. The parameters are validated by ``GuestDiskSpec`` and shell-quoted.

The one exception to "blank": a disk that already carries exactly this change's
filesystem type and label, with no partitions. The disk is found by a serial
only this change sets, so that filesystem is this change's own from an earlier
run that stopped part-way - it is never formatted again, and never wiped.
"""

from __future__ import annotations

import shlex

from homepilot.artifacts.guest_disk import GuestDiskSpec

# The in-guest steps, in order, with the words a reviewer reads.
GUEST_STEPS: tuple[tuple[str, str], ...] = (
    ("find-disk", "find the disk by its serial"),
    ("check-blank", "check it is blank (no partitions, no signature)"),
    ("mkfs", "make the filesystem with its label"),
    ("mount-point", "create the mount point (empty, immutable)"),
    ("fstab-backup", "back up /etc/fstab"),
    ("fstab-line", "append the fstab line"),
    ("mount", "mount it"),
    ("verify", "verify with findmnt"),
)

RC_OK = 0
RC_REFUSED = 3
RC_FAILED = 4

_BODY = r"""
LINE="LABEL=$LABEL $MP $FSTYPE $OPTS 0 $PASSNO"
BACKUP="$FSTAB.homepilot-$SERIAL.bak"
step() { printf 'HP-STEP %s %s %s\n' "$1" "$2" "$3"; }
fact() { printf 'HP-FACT %s %s\n' "$1" "$2"; }
refuse() { step "$1" failed "$2"; exit 3; }
broke() { step "$1" failed "$2"; exit 4; }

# Is this shell the QEMU guest agent confined by SELinux (Fedora/RHEL/Rocky/
# Alma)? Read with the shell's own `read` - nothing to exec, which a confined
# agent may not be allowed to do. Confined = our context is virt_qemu_ga_t and
# SELinux is not permissive (an unreadable enforce file counts as enforcing).
confined() {
  ctx=''; enf=''
  { read -r ctx < "$SELF_CONTEXT"; } 2>/dev/null || true
  { read -r enf < "$SELINUX_ENFORCE"; } 2>/dev/null || true
  case "$ctx" in *virt_qemu_ga_t*) [ "$enf" != 0 ];; *) return 1;; esac
}
SELINUX_WAY="An operator can widen what the guest agent may do, inside the guest: \
'setsebool -P virt_qemu_ga_run_unconfined 1' lets it run programs labelled \
virt_qemu_ga_unconfined_exec_t unconfined, and 'semanage permissive -a virt_qemu_ga_t' \
stops confining it at all - either lets whoever can reach the guest agent do more, \
so it is a decision for the guest's owner. HomePilot changes neither"
# A tool the agent's shell cannot find: really missing, or there and out of
# the agent's reach? (3.14 walk: a Fedora guest with mount installed was told
# "the guest has no mount".) A stat of the usual places tells "installed" from
# "missing"; under SELinux the stat may be refused too, so confinement is said
# on its own when the file cannot be seen.
missing() {
  at=''
  for d in $SYSDIRS; do
    if [ -e "$d/$1" ]; then at="$d/$1"; break; fi
  done
  if [ -n "$at" ] && confined; then
    refuse tools "the guest has $at, but SELinux confines the QEMU guest agent \
(domain virt_qemu_ga_t, enforcing) and the commands it starts may not run it. $SELINUX_WAY"
  fi
  if [ -n "$at" ]; then
    refuse tools "the guest has $at, but the guest agent's shell cannot run it \
(it is not on the agent's PATH, or not executable)"
  fi
  if confined; then
    refuse tools "the guest agent cannot see $1: SELinux confines it (domain \
virt_qemu_ga_t, enforcing), so $1 may be installed and out of its reach, or really \
missing${2:+ ($2)}. $SELINUX_WAY"
  fi
  refuse tools "the guest has no $1${2:+ - $2}"
}

for tool in lsblk blkid wipefs findmnt mount grep; do
  command -v "$tool" >/dev/null 2>&1 || missing "$tool" ""
done
if [ "$MODE" != verify ]; then
  command -v "mkfs.$FSTYPE" >/dev/null 2>&1 || \
    missing "mkfs.$FSTYPE" "install its $FSTYPE tools first"
fi
[ -f "$FSTAB" ] || refuse tools "the guest has no $FSTAB"

COUNT=0
DEV=''
find_dev() {
  found=$(
    lsblk -dnpr -o NAME,SERIAL 2>/dev/null | while read -r name serial; do
      if [ "$serial" = "$SERIAL" ]; then printf '%s\n' "$name"; fi
    done
    for link in "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_$SERIAL" \
                "/dev/disk/by-id/virtio-$SERIAL"; do
      if [ -e "$link" ]; then readlink -f "$link"; fi
    done
  )
  found=$(printf '%s\n' "$found" | sed '/^$/d' | sort -u)
  COUNT=$(printf '%s\n' "$found" | sed '/^$/d' | wc -l)
  DEV=''
  if [ "$COUNT" -eq 1 ]; then DEV=$found; fi
}
parts_of() { lsblk -nr -o TYPE "$1" 2>/dev/null | grep -c '^part$'; }
ours() {
  [ -n "$DEV" ] &&
  [ "$(blkid -p -o value -s TYPE "$DEV" 2>/dev/null)" = "$FSTYPE" ] &&
  [ "$(blkid -p -o value -s LABEL "$DEV" 2>/dev/null)" = "$LABEL" ] &&
  [ "$(parts_of "$DEV")" = 0 ]
}
WHY=''
blank() {
  n=$(parts_of "$DEV")
  if [ "$n" != 0 ]; then WHY="it has $n partition(s)"; return 1; fi
  if ! sigs=$(wipefs -n "$DEV" 2>&1); then WHY="wipefs could not read it: $sigs"; return 1; fi
  if [ -n "$sigs" ]; then
    WHY="it carries a signature: $(printf '%s' "$sigs" | tr '\n' ' ')"; return 1
  fi
  if blkid -p "$DEV" >/dev/null 2>&1; then WHY="blkid finds a signature on it"; return 1; fi
  return 0
}
# Lines (comments aside) that name this mount point or this label but are not ours.
conflicts() {
  grep -v '^[[:space:]]*#' "$FSTAB" | while read -r spec file rest; do
    if [ "$file" = "$MP" ] || [ "$spec" = "LABEL=$LABEL" ]; then
      if [ "$spec $file $rest" != "$LINE" ]; then printf '%s %s %s\n' "$spec" "$file" "$rest"; fi
    fi
  done
}

find_dev
if [ -z "$DEV" ] && [ "$MODE" = apply ]; then
  if command -v udevadm >/dev/null 2>&1; then udevadm settle --timeout=10 >/dev/null 2>&1; fi
  i=0
  while [ -z "$DEV" ] && [ "$COUNT" -eq 0 ] && [ "$i" -lt 30 ]; do
    sleep 1; i=$((i + 1)); find_dev
  done
fi
if [ "$COUNT" -gt 1 ]; then
  refuse find-disk "more than one device reports serial $SERIAL - refusing to guess"
fi
MNT=$(findmnt -rn -o SOURCE --mountpoint "$MP" 2>/dev/null)
MNT_DEV=''
if [ -n "$MNT" ]; then MNT_DEV=$(readlink -f "$MNT"); fi

if [ "$MODE" = verify ]; then
  fact device "${DEV:-none}"
  if [ -n "$DEV" ]; then
    fact fstype "$(blkid -p -o value -s TYPE "$DEV" 2>/dev/null)"
    fact label "$(blkid -p -o value -s LABEL "$DEV" 2>/dev/null)"
  fi
  fact mounted "${MNT_DEV:-none}"
  if grep -qxF "$LINE" "$FSTAB"; then fact fstab yes; else fact fstab no; fi
  exit 0
fi

# Checks that hold before the attach and on every retry after it.
if [ -e "$MP" ] && [ ! -d "$MP" ]; then refuse mount-point "$MP exists and is not a directory"; fi
if [ -n "$MNT" ] && { [ -z "$DEV" ] || [ "$MNT_DEV" != "$DEV" ]; }; then
  refuse mount-point "$MNT is already mounted at $MP"
fi
if [ -z "$MNT" ] && [ -d "$MP" ] && [ -n "$(ls -A "$MP" 2>/dev/null)" ]; then
  refuse mount-point "$MP is not empty - its files would be hidden under the new disk"
fi
other=$(conflicts)
if [ -n "$other" ]; then
  refuse fstab-line \
    "$FSTAB already has a line for $MP or LABEL=$LABEL: $(printf '%s' "$other" | tr '\n' ' ')"
fi
for holder in $(blkid -o device -t "LABEL=$LABEL" 2>/dev/null); do
  if [ "$(readlink -f "$holder")" != "$DEV" ]; then
    refuse check-blank "$holder already carries the label $LABEL - LABEL=$LABEL would be ambiguous"
  fi
done

if [ "$MODE" = preflight ]; then
  fact device "${DEV:-none}"
  exit 0
fi

[ -n "$DEV" ] || refuse find-disk "no disk with serial $SERIAL appeared in the guest within 30 s"
step find-disk done "$DEV has serial $SERIAL"

if ours; then
  step check-blank skipped \
    "$DEV already carries $FSTYPE label $LABEL from an earlier run of this change"
  step mkfs skipped "already made by an earlier run - not formatted again"
else
  blank || refuse check-blank "$DEV is not blank: $WHY - nothing was formatted"
  step check-blank done "no partitions, no signature"
  out=$(mkfs."$FSTYPE" -q -L "$LABEL" "$DEV" 2>&1 </dev/null) || \
    broke mkfs "mkfs.$FSTYPE failed: $out"
  if command -v udevadm >/dev/null 2>&1; then udevadm settle --timeout=10 >/dev/null 2>&1; fi
  ours || broke mkfs "after mkfs $DEV does not read back as $FSTYPE label $LABEL"
  step mkfs done "$FSTYPE, label $LABEL"
fi

if [ -n "$MNT" ]; then
  step mount-point skipped "$MP is already where $DEV is mounted"
else
  if [ ! -d "$MP" ]; then mkdir -p "$MP" || broke mount-point "could not create $MP"; fi
  if command -v chattr >/dev/null 2>&1 && chattr +i "$MP" 2>/dev/null; then
    step mount-point done \
      "$MP is empty and immutable: nothing lands in it unless the disk is mounted"
  else
    step mount-point done "$MP is empty (this filesystem cannot make it immutable)"
  fi
fi

if grep -qxF "$LINE" "$FSTAB"; then
  step fstab-backup skipped "the line is already in $FSTAB"
  step fstab-line skipped "already present"
else
  if [ -e "$BACKUP" ]; then
    step fstab-backup skipped "$BACKUP already holds the copy from before this change"
  else
    cp -p "$FSTAB" "$BACKUP" || broke fstab-backup "could not copy $FSTAB to $BACKUP"
    step fstab-backup done "$BACKUP"
  fi
  if [ -s "$FSTAB" ] && [ "$(tail -c 1 "$FSTAB" | wc -l)" -eq 0 ]; then
    printf '\n' >> "$FSTAB" || broke fstab-line "could not write $FSTAB"
  fi
  printf '%s\n' "$LINE" >> "$FSTAB" || broke fstab-line "could not append to $FSTAB"
  grep -qxF "$LINE" "$FSTAB" || broke fstab-line "the line did not read back from $FSTAB"
  step fstab-line done "$LINE"
fi
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
fi

if [ -n "$MNT" ]; then
  step mount skipped "$MP is already mounted from $DEV"
else
  out=$(mount "$MP" 2>&1) || broke mount "mount $MP failed: $out"
  step mount done "$MP"
fi

got=$(findmnt -rn -o SOURCE,FSTYPE --mountpoint "$MP" 2>/dev/null)
set -- $got
[ "$#" -ge 2 ] || broke verify "findmnt shows nothing mounted at $MP"
[ "$(readlink -f "$1")" = "$DEV" ] || broke verify "findmnt shows $1 at $MP, not $DEV"
[ "$2" = "$FSTYPE" ] || broke verify "findmnt shows $2 at $MP, not $FSTYPE"
[ "$(blkid -p -o value -s LABEL "$DEV" 2>/dev/null)" = "$LABEL" ] || \
  broke verify "$DEV lost its label"
step verify done "$MP is $DEV ($FSTYPE, label $LABEL), and $FSTAB carries the line"
exit 0
"""


def guest_script(spec: GuestDiskSpec, serial: str, mode: str) -> str:
    """The script for one mode, its parameters quoted into its first lines."""
    if mode not in ("preflight", "apply", "verify"):
        raise ValueError(f"unknown guest-disk script mode {mode!r}")
    passno = "0" if spec.filesystem == "xfs" else "2"
    header = [
        "set -u",
        f"MODE={shlex.quote(mode)}",
        f"SERIAL={shlex.quote(serial)}",
        f"LABEL={shlex.quote(spec.label)}",
        f"FSTYPE={shlex.quote(spec.filesystem)}",
        f"MP={shlex.quote(spec.mount_point)}",
        f"OPTS={shlex.quote(spec.mount_options)}",
        f"PASSNO={passno}",
        "FSTAB='/etc/fstab'",
        # Where the SELinux facts and the usual tool directories are (a test
        # guest points these at files of its own, as it does FSTAB).
        "SELF_CONTEXT='/proc/self/attr/current'",
        "SELINUX_ENFORCE='/sys/fs/selinux/enforce'",
        "SYSDIRS='/usr/sbin /usr/bin /sbin /bin /usr/local/sbin /usr/local/bin'",
    ]
    return "\n".join(header) + _BODY


def parse_output(out: str) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """``(steps, facts)`` from a run's stdout: ``(id, status, words)`` and ``key -> value``."""
    steps: list[tuple[str, str, str]] = []
    facts: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("HP-STEP "):
            parts = line[8:].split(" ", 2)
            if len(parts) >= 2:
                steps.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
        elif line.startswith("HP-FACT "):
            key, _, value = line[8:].partition(" ")
            facts[key] = value
    return steps, facts
