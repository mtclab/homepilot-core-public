"""A guest that REALLY RUNS the shell HomePilot sends it (#628).

Every previous fake for this path answered `agent_run` with a tuple somebody
typed, which is why a green suite never noticed that

    curl -fsSL https://tailscale.com/install.sh | sh

cannot fail: a pipeline's exit status is its LAST command's, so a download that
404s feeds `sh` an empty script and `sh` exits 0. No assertion about a string
finds that. Running the string does, in one line.

So this runs the actual script through `/bin/sh`, with `PATH` pointing at a
directory of small fake binaries the test composes: a `curl` that succeeds or
fails as the test says, a `getent` that resolves or does not, a `tailscale` that
appears only once the installer has "installed" it. What the script decides -
which fetcher to use, whether the download produced anything, whether the binary
is there afterwards, which exit code comes back - is decided by `sh`, not by us.

`env -i` and a PATH of exactly one directory is what makes "the guest has no
curl" testable at all: the scripts deliberately do not name system directories
of their own (see the comment on `_TAILSCALE_PROBE_SCRIPT`), so the only PATH in
play is the one the caller sets.

The one thing simulated rather than executed is the KEY FILE path: the join
script stages the auth key at `/run/hp-tailscale.key`, which a test cannot
write. Its literal path is rewritten to a file under tmp_path for execution, and
the ORIGINAL script text is what the tests assert on - so the shell semantics
(the `cat`, the `rm -f`, the quoting, the exit status) are all still real.
"""

from __future__ import annotations

import subprocess  # nosec B404 - running the guest's own shell IS the test
from pathlib import Path
from typing import Any

from homepilot.adapters.proxmox import ProxmoxError

KEY_PATH = "/run/hp-tailscale.key"

# A fetcher that writes an installer which installs a working-looking tailscale.
_GOOD_INSTALLER = """#!/bin/sh
cat > "$HP_BIN/tailscale" <<'EOF'
#!/bin/sh
exit ${HP_TAILSCALE_UP_RC:-0}
EOF
chmod +x "$HP_BIN/tailscale"
"""

# An installer that exits 0 and installs nothing at all. Real: the vendor script
# takes this shape on a distribution it does not recognise.
_EMPTY_INSTALLER = """#!/bin/sh
exit 0
"""


class ShellGuest:
    """One guest, with a shell, a PATH and a set of binaries the test chooses."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        has_tailscale: bool = False,
        fetcher: str | None = "curl",
        fetch_ok: bool = True,
        fetch_error: str = "",
        dns_ok: bool = True,
        has_getent: bool = True,
        installer_installs: bool = True,
        installer_rc: int = 0,
        tailscale_up_rc: int = 0,
        tailscale_up_stderr: str = "",
        agent_ready_after: int = 0,
        exec_error: str | None = None,
        selinux_confined: bool = False,
    ) -> None:
        self.bin = tmp_path / "guestbin"
        self.bin.mkdir(exist_ok=True)
        self.key_file = tmp_path / "hp-tailscale.key"
        self.scripts: list[str] = []
        self.tailscale_up_rc = tailscale_up_rc
        self.tailscale_up_stderr = tailscale_up_stderr
        # How many pings the guest ignores before its agent starts answering.
        self.agent_ready_after = agent_ready_after
        self.pings = 0
        # A guest whose qemu-guest-agent refuses guest-exec: the config says the
        # agent is enabled, the ping comes back, and every exec is refused.
        self.exec_error = exec_error

        if has_tailscale:
            self._write(
                "tailscale",
                "#!/bin/sh\nexit ${HP_TAILSCALE_UP_RC:-0}\n",
            )
        if fetcher == "curl":
            self._write(
                "curl", self._fetcher_body(fetch_ok, installer_installs, installer_rc, fetch_error)
            )
        elif fetcher == "wget":
            self._write(
                "wget", self._fetcher_body(fetch_ok, installer_installs, installer_rc, fetch_error)
            )
        elif fetcher == "python3":
            # The python3 branch writes the installer to STDOUT, which the script
            # redirects; the others take an output path. Both shapes are the real
            # ones, so the fake keeps them apart.
            body = _GOOD_INSTALLER if installer_installs else _EMPTY_INSTALLER
            if installer_rc:
                body += f"exit {installer_rc}\n"
            if fetch_ok:
                self._write("python3", "#!/bin/sh\ncat <<'INSTALLER'\n" + body + "INSTALLER\n")
            else:
                self._write("python3", "#!/bin/sh\nexit 1\n")
        if has_getent:
            self._write("getent", f"#!/bin/sh\nexit {0 if dns_ok else 2}\n")
        # A guest whose qemu-guest-agent is CONFINED by SELinux. `id -Z` is how
        # the installer tells this apart from a network fault, because from the
        # fetcher's point of view they are identical: proven live on dev, a
        # Fedora guest's agent reaches 1.1.1.1:53 and gets EPERM on :443.
        if selinux_confined:
            self._write("id", "#!/bin/sh\necho system_u:system_r:virt_qemu_ga_t:s0\n")
        else:
            self._write("id", "#!/bin/sh\necho unconfined_u:unconfined_r:unconfined_t:s0\n")
        # Utilities every image has and the scripts use unconditionally.
        for name in ("sh", "cat", "rm", "chmod", "mkdir", "grep"):
            real = f"/usr/bin/{name}" if Path(f"/usr/bin/{name}").exists() else f"/bin/{name}"
            self._write(name, f'#!/bin/sh\nexec {real} "$@"\n')

    def _fetcher_body(
        self, fetch_ok: bool, installs: bool, installer_rc: int, fetch_error: str = ""
    ) -> str:
        if not fetch_ok:
            # curl's own "couldn't connect" code, and whatever it said on the way
            # out. The script must not read this as anything but a failed fetch,
            # and the words must survive to the reason the caller is shown.
            said = f"echo '{fetch_error}' >&2\n" if fetch_error else ""
            return "#!/bin/sh\n" + said + "exit 7\n"
        body = _GOOD_INSTALLER if installs else _EMPTY_INSTALLER
        if installer_rc:
            body += f"exit {installer_rc}\n"
        # Both curl -o FILE and wget -O FILE put the path last-but-one; taking
        # the last argument that looks like a path is enough for either.
        return (
            '#!/bin/sh\nout=\nfor a in "$@"; do case "$a" in /*) out=$a;; esac; done\n'
            "cat > \"$out\" <<'INSTALLER'\n" + body + "INSTALLER\n"
        )

    def _write(self, name: str, body: str | None) -> None:
        path = self.bin / name
        path.write_text(body or "#!/bin/sh\nexit 0\n")
        path.chmod(0o755)

    # ── the PVE guest-agent surface, as ProxmoxClient calls it ──────────────

    async def agent_ping(self, node: str, vmid: int) -> bool:
        self.pings += 1
        return self.pings > self.agent_ready_after

    async def agent_write_file(self, node: str, vmid: int, path: str, content: str) -> Any:
        assert path == KEY_PATH, f"the key must be staged at {KEY_PATH}, not {path}"
        self.key_file.write_text(content)
        return {"data": {}}

    async def agent_run(
        self, node: str, vmid: int, script: str, timeout_s: float = 300.0, **_: Any
    ) -> tuple[int, str, str]:
        self.scripts.append(script)
        if self.exec_error is not None:
            raise ProxmoxError("POST", "/agent/exec", 500, self.exec_error)
        runnable = script.replace(KEY_PATH, str(self.key_file))
        env = {
            "PATH": str(self.bin),
            "HP_BIN": str(self.bin),
            "HP_TAILSCALE_UP_RC": str(self.tailscale_up_rc),
        }
        proc = subprocess.run(  # nosec B603 - a fixed argv, no shell, in a temp dir
            ["/bin/sh", "-c", runnable],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        err = proc.stderr
        if "tailscale up" in script and self.tailscale_up_rc != 0:
            err = self.tailscale_up_stderr or err
        return proc.returncode, proc.stdout, err

    def key_file_exists(self) -> bool:
        return self.key_file.exists()

    async def get_vm_current(self, node: str, vmid: int) -> dict[str, Any]:
        """This guest EXISTS and is running.

        Modelled explicitly because the join now asks before naming a cause: an
        agent that says nothing has more than one explanation, and a fake that
        cannot answer "is the machine even there" would let the code take a
        branch no real cluster would give it. These guests are here and running
        - a silent agent on one of them really is the template's fault, which
        is what the tests using this fake are about.
        """
        return {"data": {"status": "running", "vmid": vmid}}

    def bind(self, proxmox: Any) -> ShellGuest:
        """Put this guest behind a mocked ProxmoxClient's agent surface."""
        proxmox.agent_ping = self.agent_ping
        proxmox.agent_run = self.agent_run
        proxmox.agent_write_file = self.agent_write_file
        proxmox.get_vm_current = self.get_vm_current
        return self
