"""A Proxmox VM with disks, and a guest that REALLY RUNS the shell HomePilot sends it (#824).

The cluster is ``FakeCluster`` from the power gate (the one fake, at
``ProxmoxClient.call``) taught what an added disk needs, modelled on what PVE
does:

* ``POST .../qemu/<vmid>/config`` with ``scsiN: <storage>:<GiB>,serial=...``
  allocates a volume and rewrites the value the way PVE stores it
  (``<storage>:vm-<vmid>-disk-<n>,serial=...,size=<n>G``). With ``disk`` in the
  guest's ``hotplug`` (absent = the default ``network,disk,usb``) and the guest
  running, the disk lands in the CURRENT config and appears inside the guest
  with that serial; otherwise PVE parks it in the PENDING config and the guest
  sees nothing;
* ``/nodes/<n>/storage`` lists storages with content types and free space;
* the QEMU guest agent: ``agent/ping``, ``agent/exec`` (argv) and
  ``agent/exec-status`` (exited / exitcode / out-data / err-data).

``agent/exec`` runs the script through ``/bin/sh`` with ``PATH`` set to ONE
directory of small fake tools that keep the guest's disks in files: ``lsblk``
(NAME/SERIAL, TYPE), ``blkid -p`` (TYPE, LABEL, signature), ``blkid -t LABEL=``,
``wipefs -n``, ``mkfs.ext4`` / ``mkfs.xfs`` (logged), ``mount <mountpoint>``
(reads the real fstab line, resolves ``LABEL=``), ``findmnt``, ``chattr``. What
the script decides - which device, blank or not, whether to format, what goes
into fstab - is decided by ``sh``. The coreutils it uses are the real ones. The
one thing rewritten is the fstab PATH (``/etc/fstab`` -> a file under
tmp_path); the mount point is a real directory under tmp_path.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - running the guest's own shell IS the test
from pathlib import Path
from typing import Any

from homepilot.adapters.proxmox import ProxmoxError
from tests.test_power_controls import NODE, FakeCluster

GIB = 1024**3
ROOT_FSTAB = "UUID=0000-root / ext4 errors=remount-ro 0 1\n"

_REAL_TOOLS = (
    "grep",
    "sed",
    "sort",
    "wc",
    "readlink",
    "ls",
    "cat",
    "tail",
    "cp",
    "mkdir",
    "tr",
    "basename",
    "head",
)

_FAKES: dict[str, str] = {
    "lsblk": r"""
case "$*" in
  "-dnpr -o NAME,SERIAL")
    for d in "$S"/disks/*; do
      [ -d "$d" ] || continue
      printf '/dev/%s %s\n' "$(basename "$d")" "$(cat "$d/serial")"
    done ;;
  "-nr -o TYPE "*)
    dev=$4; d="$S/disks/$(basename "$dev")"
    [ -d "$d" ] || { echo "lsblk: $dev: not a block device" >&2; exit 32; }
    echo disk
    n=$(cat "$d/parts"); i=0
    while [ "$i" -lt "$n" ]; do echo part; i=$((i + 1)); done ;;
  *) echo "fake lsblk: unexpected $*" >&2; exit 99 ;;
esac
""",
    "blkid": r"""
if [ "$1" = -p ] && [ "$2" = -o ]; then
  d="$S/disks/$(basename "$6")"
  case "$5" in TYPE) v=$(cat "$d/fstype");; LABEL) v=$(cat "$d/label");; *) exit 99;; esac
  [ -n "$v" ] || exit 2
  printf '%s\n' "$v"; exit 0
fi
if [ "$1" = -p ]; then
  d="$S/disks/$(basename "$2")"
  if [ -s "$d/fstype" ] || [ -s "$d/sig" ]; then echo "$2: TYPE=\"$(cat "$d/fstype" "$d/sig")\""; exit 0; fi
  exit 2
fi
if [ "$1" = -o ] && [ "$3" = -t ]; then
  want=${4#LABEL=}; hit=2
  for d in "$S"/disks/*; do
    if [ "$(cat "$d/label")" = "$want" ]; then echo "/dev/$(basename "$d")"; hit=0; fi
  done
  exit $hit
fi
echo "fake blkid: unexpected $*" >&2; exit 99
""",
    "wipefs": r"""
[ "$1" = -n ] || { echo "fake wipefs: only -n is allowed, got $*" >&2; exit 99; }
d="$S/disks/$(basename "$2")"
[ -d "$d" ] || { echo "wipefs: error: $2: probing initialization failed" >&2; exit 1; }
if [ -s "$d/fstype" ] || [ -s "$d/sig" ]; then
  echo "DEVICE OFFSET TYPE UUID LABEL"
  echo "$(basename "$2") 0x438 $(cat "$d/fstype" "$d/sig") 1234 $(cat "$d/label")"
fi
exit 0
""",
    "mkfs.ext4": r"""
echo "mkfs.ext4 $*" >> "$S/mkfs.log"
[ -e "$S/fail_mkfs" ] && { echo "mke2fs: Device size reported to be zero." >&2; exit 1; }
[ "$1" = -q ] && [ "$2" = -L ] || exit 99
d="$S/disks/$(basename "$4")"; printf ext4 > "$d/fstype"; printf '%s' "$3" > "$d/label"
""",
    "mkfs.xfs": r"""
echo "mkfs.xfs $*" >> "$S/mkfs.log"
[ "$1" = -q ] && [ "$2" = -L ] || exit 99
d="$S/disks/$(basename "$4")"; printf xfs > "$d/fstype"; printf '%s' "$3" > "$d/label"
""",
    "findmnt": r"""
mp=$5
while read -r dev at fs; do
  if [ "$at" = "$mp" ]; then
    if [ "$3" = SOURCE,FSTYPE ]; then echo "$dev $fs"; else echo "$dev"; fi
    exit 0
  fi
done < "$S/mounts"
exit 1
""",
    "mount": r"""
echo "mount $*" >> "$S/mount.log"
[ "$#" -eq 1 ] || { echo "fake mount: expected one mount point, got $*" >&2; exit 99; }
[ -e "$S/fail_mount" ] && { echo "mount: $1: wrong fs type, bad option, bad superblock" >&2; exit 32; }
line=$(grep -v '^[[:space:]]*#' "$FSTAB" | while read -r spec file fs rest; do
  [ "$file" = "$1" ] && echo "$spec $fs"
done)
[ -n "$line" ] || { echo "mount: $1: can't find in /etc/fstab." >&2; exit 1; }
set -- $line "$1"
label=${1#LABEL=}
for d in "$S"/disks/*; do
  if [ "$(cat "$d/label")" = "$label" ]; then echo "/dev/$(basename "$d") $3 $2" >> "$S/mounts"; exit 0; fi
done
echo "mount: can't find LABEL=$label" >&2; exit 1
""",
    "chattr": r"""
echo "chattr $*" >> "$S/chattr.log"
""",
    "sleep": "\n",
}


class DiskGuest:
    """The inside of one guest: its disks, fstab, mounts and tools, in files."""

    def __init__(self, root: Path, *, has_xfs: bool = True) -> None:
        self.root = root
        self.state = root / "state"
        self.bin = root / "bin"
        self.fstab = root / "etc" / "fstab"
        for d in (self.state / "disks", self.bin, self.fstab.parent):
            d.mkdir(parents=True, exist_ok=True)
        self.fstab.write_text(ROOT_FSTAB)
        (self.state / "mounts").write_text("/dev/sda / ext4\n")
        self._letter = ord("a")
        self.add_device("QM-ROOT-0", fstype="ext4", label="")
        prelude = f'#!/bin/sh\nS="{self.state}"\nFSTAB="{self.fstab}"\n'
        for name, text in _FAKES.items():
            if name == "mkfs.xfs" and not has_xfs:
                continue
            tool = self.bin / name
            tool.write_text(prelude + text)
            tool.chmod(0o755)
        for name in _REAL_TOOLS:
            real = shutil.which(name)
            assert real, f"the test host has no {name}"
            (self.bin / name).symlink_to(real)

    def add_device(
        self, serial: str, *, fstype: str = "", label: str = "", parts: int = 0, sig: str = ""
    ) -> str:
        name = f"sd{chr(self._letter)}"
        self._letter += 1
        d = self.state / "disks" / name
        d.mkdir()
        (d / "serial").write_text(serial)
        (d / "fstype").write_text(fstype)
        (d / "label").write_text(label)
        (d / "parts").write_text(str(parts))
        (d / "sig").write_text(sig)
        return f"/dev/{name}"

    def device_for(self, serial: str) -> str | None:
        for d in sorted((self.state / "disks").iterdir()):
            if (d / "serial").read_text() == serial:
                return f"/dev/{d.name}"
        return None

    def fs_of(self, dev: str) -> tuple[str, str]:
        d = self.state / "disks" / Path(dev).name
        return (d / "fstype").read_text(), (d / "label").read_text()

    @property
    def mounts(self) -> list[str]:
        return (self.state / "mounts").read_text().splitlines()

    def log(self, name: str) -> list[str]:
        path = self.state / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []

    def fail(self, what: str) -> None:
        (self.state / f"fail_{what}").write_text("1")

    def run(self, script: str) -> tuple[int, str, str]:
        assert "FSTAB='/etc/fstab'" in script, "the guest script names the real fstab"
        script = script.replace("FSTAB='/etc/fstab'", f"FSTAB='{self.fstab}'")
        proc = subprocess.run(  # nosec B603 - the script under test, a fixed interpreter
            ["/bin/sh", "-c", script],
            env={"PATH": str(self.bin), "HOME": str(self.root), "LC_ALL": "C"},
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr


class DiskCluster(FakeCluster):
    """``FakeCluster`` plus storages, disk attach and the QEMU guest agent."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__()
        self.tmp = tmp_path
        self.storages: list[dict[str, Any]] = [
            {
                "storage": "fast-pool",
                "type": "lvmthin",
                "content": "images,rootdir",
                "active": 1,
                "enabled": 1,
                "avail": 400 * GIB,
                "total": 900 * GIB,
            },
            {
                "storage": "local",
                "type": "dir",
                "content": "iso,vztmpl,backup",
                "active": 1,
                "enabled": 1,
                "avail": 50 * GIB,
                "total": 100 * GIB,
            },
        ]
        self.guests_inside: dict[int, DiskGuest] = {}
        self.agent_alive: dict[int, bool] = {}
        self.execs: list[str] = []
        self._exec_results: dict[int, dict[str, Any]] = {}
        self._volumes = 0

    def add_vm(self, vmid: int, name: str, *, config: dict[str, Any] | None = None) -> DiskGuest:
        base = {
            "agent": "1",
            "scsihw": "virtio-scsi-single",
            "scsi0": f"fast-pool:vm-{vmid}-disk-0,iothread=1,size=32G",
            "meta": "creation-qemu=9.0.2,ctime=1715000000",
            "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0",
        }
        base.update(config or {})
        for key in [k for k, v in base.items() if v is None]:
            del base[key]
        self.add(vmid, name, "running", base)
        inside = DiskGuest(self.tmp / f"guest-{vmid}")
        self.guests_inside[vmid] = inside
        self.agent_alive[vmid] = True
        return inside

    @property
    def config_writes(self) -> list[tuple[str, dict[str, Any] | None]]:
        return [(p, b) for p, b in self.posts if p.endswith("/config")]

    def _attach(self, vmid: int, body: dict[str, Any]) -> str:
        g = self.guests[vmid]
        hotplug = str(g["config"].get("hotplug", "network,disk,usb"))
        live = g["status"] == "running" and (hotplug == "1" or "disk" in hotplug.split(","))
        for key, value in body.items():
            spec, _, opts = str(value).partition(",")
            storage, _, size = spec.partition(":")
            row = next((s for s in self.storages if s["storage"] == storage), None)
            if row is None:
                raise ProxmoxError("POST", "config", 500, f"storage '{storage}' does not exist")
            self._volumes += 1
            stored = f"{storage}:vm-{vmid}-disk-{self._volumes}," + (opts + "," if opts else "")
            stored += f"size={size}G"
            row["avail"] -= int(size) * GIB
            if live:
                g["config"][key] = stored
                serial = next(
                    (p[7:] for p in opts.split(",") if p.startswith("serial=")), "QM0000" + key
                )
                self.guests_inside[vmid].add_device(serial)
            else:
                g["pending"][key] = stored
        return self._task(g["node"], "OK")

    def _exec(self, vmid: int, command: list[str]) -> dict[str, Any]:
        assert command[:2] == ["sh", "-c"], command
        self.execs.append(command[2])
        rc, out, err = self.guests_inside[vmid].run(command[2])
        pid = len(self.execs)
        self._exec_results[pid] = {"exited": 1, "exitcode": rc, "out-data": out, "err-data": err}
        return {"pid": pid}

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = "/" + path.lstrip("/")
        parts = path.strip("/").split("/")
        if method == "GET" and path == f"/nodes/{NODE}/storage":
            return {"data": [dict(s) for s in self.storages]}
        if len(parts) >= 5 and parts[2] == "qemu" and parts[4] == "agent":
            vmid = int(parts[3])
            if not self.agent_alive.get(vmid):
                raise ProxmoxError(method, path, 500, "QEMU guest agent is not running")
            op = parts[5]
            if op == "ping":
                return {"data": {}}
            if op == "exec":
                assert set(body or {}) == {"command"}, "exec takes only `command` (#628)"
                return {"data": self._exec(vmid, list((body or {})["command"]))}
            if op == "exec-status":
                return {"data": self._exec_results[int((query or {})["pid"])]}
        if method == "POST" and len(parts) == 5 and parts[2] == "qemu" and parts[4] == "config":
            self.posts.append((path, body))
            return {"data": self._attach(int(parts[3]), dict(body or {}))}
        return await super().call(method, path, body, query)


def guest_paths(inside: DiskGuest) -> str:
    """A mount point for this guest: a real, absent directory under its root."""
    mp = inside.root / "srv" / "data"
    assert os.path.normpath(mp) == str(mp)
    return str(mp)
