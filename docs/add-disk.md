# Add a disk to a VM

A VM's disk is filling up, or a service needs room of its own. HomePilot adds a
new disk and takes it into use as **one change you approve** (#824):

1. **Attach** a new disk from a Proxmox storage to the running VM - hot-plugged,
   on the first free slot of the bus, with a serial number HomePilot chooses.
2. **Inside the guest**, as root through the QEMU guest agent: find the disk by
   that serial, refuse unless it is blank, make a filesystem with a label,
   create the mount point, back up `/etc/fstab`, add one line by `LABEL=` with
   `nofail`, mount that one mount point and check it with `findmnt`.

Nothing runs until you approve the change. The in-guest part runs only through
an approved change, and only on the blank disk that change just attached.

## Where

- **Hosts > a VM > Add a disk.** Pick the storage (the list is the node's
  storages that hold VM disks, with their free space), the size, the filesystem,
  the mount point and the label. "This storage is on SSDs" attaches the disk with
  `discard` (and `ssd=1` on scsi) and mounts it with `discard`. **Propose the
  change** opens its review, which shows what applying does now: the slot it
  will take, the serial, the fstab line, every step, and anything that would
  refuse it.
- **Assistants (MCP):** `get_guest_disks` reads the same options;
  `add_guest_disk` proposes the change. Neither attaches anything.
- **CLI:** `hp changes propose add-disk.yaml` with a spec like the one below.

## What it needs

- A **QEMU VM** that is **running**. A container is refused: the filesystem is
  made through the QEMU guest agent, which a container does not have.
- **Disk hotplug** on (the VM's Options > Hotplug includes Disk; Proxmox's
  default). Without it Proxmox would only add the disk at the next restart, so
  the change is refused before anything is sent.
- The **QEMU guest agent** enabled for the VM and answering inside it, the same
  agent the zero-touch agent install uses.
- In the guest: `lsblk`, `blkid`, `wipefs`, `findmnt`, `mount` and
  `mkfs.ext4` (or `mkfs.xfs` for xfs).
- A storage with the `images` content type and enough free space.
- A mount point that does not exist yet or is an empty directory, and a label
  and mount point no fstab line uses yet.

Everything on that list is checked before anything changes - at propose (the
form and `add_guest_disk`), in the review, and again when the change runs.

## A physical disk

A whole physical disk (`/dev/disk/by-id/...`) is not something HomePilot can
attach: Proxmox lets only root on the host pass a device path, and refuses an
API token ("only root can pass arbitrary filesystem paths"). Asking for one is
refused with the exact command to run as root on the node, for example:

```
qm set 101 --scsi1 /dev/disk/by-id/ata-EXAMPLE_SSD_0001,serial=hp0123456789ab
```

HomePilot never runs that command.

## Why the guest agent, not the hp-agent

The in-guest part needs root: `mkfs`, `/etc/fstab`, `mount`. The hp-agent is
unprivileged by design, and giving it a root grant would give every caller of
the agent hub a standing root path into the guest. The QEMU guest agent is
reached only through the Proxmox API with HomePilot's token, already runs as
root, is how HomePilot installs the hp-agent in the first place, and here runs
exactly one fixed script, sent only by an approved change.

## How the disk is found

By the serial the attach sets (`serial=hp...` on the new disk), which the guest
sees in `lsblk -o SERIAL` and `/dev/disk/by-id/`. Never by a `/dev/sdX` letter:
the order in which a kernel names disks is not stable. More than one device with
that serial is refused.

## Blank, or refused

Before `mkfs` the disk must have no partitions and no signature (`wipefs -n`
lists nothing, `blkid -p` finds nothing). A disk that fails that is refused and
**nothing is formatted**. The one exception is a disk that already carries this
change's own filesystem type and label from an earlier run of the same change
that stopped part-way: it is used as it is, never formatted again.

The mount point is made immutable while it is empty (`chattr +i`, where the
filesystem supports it), so that if the disk is ever missing at boot - `nofail`
lets the guest boot anyway - nothing can silently write into the root disk in
its place.

## When it partly works

If the attach works and the in-guest part fails, the change ends **partly
applied**: it names the steps that were done, the one that failed and why, and
the steps not run. **The disk stays attached** - HomePilot never detaches a disk
by itself. What you can do:

- **Retry:** fix what the guest reported, approve the change again and apply it.
  The disk is found by its serial and not attached a second time; every in-guest
  step already done is skipped.
- **Take the disk off instead:** in Proxmox, the VM's Hardware > the slot >
  Detach, then remove the unused disk (on the node: `qm set <vmid> --delete
  <slot>`). If the fstab line was already added, restore
  `/etc/fstab.homepilot-<serial>.bak` in the guest first.

## Afterwards

Drift checks that the disk with the serial is still attached, that the guest
sees it mounted at the mount point from the right label and type, and that
fstab still carries the line. It does that with a read-only run of the same
script (`lsblk`, `blkid -p`, `findmnt`, `grep`); a guest that cannot be asked
reads as unknown, never as in spec. Re-applying a change that is in spec changes
nothing.

Revoking the change relabels it and leaves the disk where it is.

Not part of this change: moving data onto the new disk, or switching an
existing directory over to it. Those need a downtime step you run yourself.

## Example spec (CLI)

```yaml
kind: guest-disk
intent: Add a 50 GiB disk to app-01 at /srv/data
target: {kind: vm, host: app-01, vmid: 101, node: pve1}
idempotence: via-precheck
requires_snapshot: false
body: |
  Add a data disk for the application's uploads.

  ```yaml guest-disk-spec
  storage: fast-pool
  size_gib: 50
  filesystem: ext4
  mount_point: /srv/data
  label: appdata
  mount_options: defaults,nofail,discard
  ssd: true
  ```
```

`serial` may be left out: the change then uses one derived from its id. Fields:
`storage`, `size_gib` (1-65536), `filesystem` (`ext4`, default, or `xfs`),
`mount_point` (absolute, not `/` and not under `/etc`, `/usr`, `/boot` and the
other system directories), `label` (up to 16 characters for ext4, 12 for xfs),
`mount_options` (must keep `nofail`), `bus` (`scsi`, default, or `virtio`),
`ssd`, `serial`. The kind rules: `target.kind: vm` with the guest's name,
`idempotence: via-precheck`, `requires_snapshot: false`, no rollback.

A spec proposed from the CLI is validated, but the live checks (hotplug, the
guest agent, the storage, the mount point) run when you open its review and
again when it runs; the form and `add_guest_disk` run them before proposing.
