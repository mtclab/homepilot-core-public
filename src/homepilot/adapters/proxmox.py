from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# The two collections PVE files guests under. A VMID alone does not say which
# one a guest lives in, so every by-vmid path has to resolve the type first.
GUEST_TYPES = ("qemu", "lxc")

# Artifact target kinds that name a guest, mapped onto the PVE collection. PVE
# spellings map to themselves so a caller may pass either vocabulary.
TARGET_KIND_GUEST_TYPES = {"vm": "qemu", "qemu": "qemu", "lxc": "lxc"}


class ProxmoxError(Exception):
    def __init__(
        self, method: str, path: str, status_code: int, body: str, hint: str | None = None
    ):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        self.hint = hint
        message = f"{method} {path} -> {status_code}: {body[:200]}"
        if hint:
            message = f"{message} ({hint})"
        super().__init__(message)


class ProxmoxClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        verify_ssl: bool = True,
        write_token: str | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._write_token = write_token or token
        self._has_separate_write = write_token is not None and write_token != token
        self._verify_ssl = verify_ssl
        verify = verify_ssl
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/api2/json",
            headers={"Authorization": f"PVEAPIToken={self._token}"},
            verify=verify,
            timeout=httpx.Timeout(30.0),
        )
        if self._has_separate_write:
            self._write_client = httpx.AsyncClient(
                base_url=f"{self._base_url}/api2/json",
                headers={"Authorization": f"PVEAPIToken={self._write_token}"},
                verify=verify,
                timeout=httpx.Timeout(30.0),
            )
        else:
            self._write_client = self._client

    def credentials(self) -> Any:
        """This client's own connection details, for building a sibling client.

        The guest-network work talks to PVE through the estate's
        ``proxmox_mcp`` library rather than through this class (which gains no
        new endpoint knowledge, by owner mandate), and that library needs the
        same host and the same tokens. Handing them over here is what keeps
        ONE configuration surface instead of two.

        Carries live secrets: never log it, never put it in a response, never
        serialise it. It exists to be passed to a client constructor.
        """
        from .pve_sdn import PveCredentials

        return PveCredentials(
            base_url=self._base_url,
            token=self._token,
            write_token=self._write_token,
            verify_ssl=self._verify_ssl,
        )

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = path.lstrip("/")
        method_upper = method.upper()
        is_mutation = method_upper in ("POST", "PUT", "PATCH", "DELETE")
        client = self._write_client if is_mutation else self._client
        try:
            response = await client.request(
                method=method_upper,
                url=f"/{path}",
                json=body,
                params=query,
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            try:
                resp_body = exc.response.text
            except (UnicodeDecodeError, httpx.ResponseNotRead):
                resp_body = "<unreadable>"
            raise ProxmoxError(
                method=method_upper,
                path=path,
                status_code=status,
                body=resp_body,
            ) from exc
        except httpx.RequestError as exc:
            raise ProxmoxError(
                method=method_upper,
                path=path,
                status_code=0,
                body=str(exc),
            ) from exc

    async def read(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.call("GET", path, query=query)

    async def resolve_guest_type(self, vmid: int) -> str:
        """Which collection a VMID lives in on THIS cluster - "qemu" or "lxc".

        A VMID says nothing about its guest type, so anything addressing a guest
        by id has to ask. `/cluster/resources?type=vm` carries the type of every
        guest and template in the cluster, which settles it in one read.
        """
        result = await self.read("/cluster/resources", query={"type": "vm"})
        rows = result.get("data", result)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    row_vmid = int(row["vmid"])
                except (KeyError, TypeError, ValueError):
                    continue
                if row_vmid != vmid:
                    continue
                guest_type = str(row.get("type", ""))
                if guest_type in GUEST_TYPES:
                    return guest_type
        raise ProxmoxError(
            "GET",
            "/cluster/resources",
            404,
            f"no guest with vmid {vmid} on this cluster, so its type "
            "(qemu or lxc) cannot be resolved",
        )

    async def _guest_type_for(self, vmid: int, guest_type: str | None) -> str:
        """The caller's stated guest type, else the cluster's answer.

        Accepts the artifact target vocabulary ("vm"/"lxc") as well as the PVE
        one ("qemu"/"lxc"); anything else is ignored in favour of asking PVE.
        """
        resolved = TARGET_KIND_GUEST_TYPES.get((guest_type or "").lower())
        if resolved is not None:
            return resolved
        return await self.resolve_guest_type(vmid)

    @staticmethod
    def _snapshot_error(exc: ProxmoxError, guest_type: str) -> ProxmoxError:
        """The same failure, said out loud.

        A snapshot failure used to surface as a bare status against whichever
        collection was tried last, which named neither the collection nor the
        credential as the possible cause (#617). Say both.
        """
        hint = f"guest resolved as {guest_type}"
        if exc.status_code in (401, 403):
            hint += (
                "; PVE rejected the credential, so the write token may lack "
                "snapshot rights on this guest (VM.Snapshot / VM.Audit) or may "
                "not be valid for this cluster"
            )
        return ProxmoxError(
            method=exc.method,
            path=exc.path,
            status_code=exc.status_code,
            body=exc.body,
            hint=hint,
        )

    async def snapshot(
        self, node: str, vmid: int, name: str, guest_type: str | None = None
    ) -> dict[str, Any]:
        resolved = await self._guest_type_for(vmid, guest_type)
        try:
            return await self.call(
                "POST",
                f"/nodes/{node}/{resolved}/{vmid}/snapshot",
                body={"snapname": name},
            )
        except ProxmoxError as exc:
            raise self._snapshot_error(exc, resolved) from exc

    async def delete_snapshot(
        self, node: str, vmid: int, name: str, guest_type: str | None = None
    ) -> dict[str, Any]:
        resolved = await self._guest_type_for(vmid, guest_type)
        try:
            return await self.call(
                "DELETE",
                f"/nodes/{node}/{resolved}/{vmid}/snapshot/{name}",
            )
        except ProxmoxError as exc:
            raise self._snapshot_error(exc, resolved) from exc

    async def clone_vm(
        self,
        node: str,
        template_vmid: int,
        new_vmid: int,
        name: str,
        full: bool = True,
        pool: str | None = None,
        storage: str | None = None,
    ) -> str:
        """Clone a template into a new VM. Returns the UPID of the clone task.

        ``storage`` targets the clone's disks at a storage other than the
        template's (#618). It is sent ONLY when given: PVE's own behaviour for
        an absent `storage` is "put the disks where the template's are", and
        that inherit is what every install had before the option existed, so
        nothing is guessed on the caller's behalf. PVE only honours it on a
        FULL clone - a linked clone shares the template's disks and so cannot
        leave its storage - which is why ``full`` defaults to True and this
        product never sends a linked clone.
        """
        body: dict[str, Any] = {
            "newid": new_vmid,
            "name": name,
            "full": 1 if full else 0,
        }
        if pool:
            body["pool"] = pool
        if storage:
            body["storage"] = storage
        result = await self.call("POST", f"/nodes/{node}/qemu/{template_vmid}/clone", body=body)
        return str(result.get("data", ""))

    @staticmethod
    def upid_of(result: Any) -> str | None:
        """The UPID inside a PVE response, or None when the call was synchronous.

        PVE answers some calls with a task id and others with ``null`` (a config
        write that needed no worker). Treating "no UPID" as an error would fail
        perfectly good runs; waiting on a non-UPID string would hang. So: a
        string that starts with ``UPID:`` is a task, anything else is not.
        """
        data = result.get("data", result) if isinstance(result, dict) else result
        if isinstance(data, str) and data.startswith("UPID:"):
            return data
        return None

    async def wait_for_task(
        self,
        node: str,
        upid: str,
        timeout_s: float = 600.0,
        poll_interval: float = 2.0,
    ) -> dict[str, Any]:
        """Block until a PVE task reaches status 'stopped'; return its status dict.

        Raises ProxmoxError if the task ends with a non-OK exitstatus, or if the
        timeout elapses first — a caller must never mistake "still running" or
        "failed" for success.
        """
        # A UPID contains ':' separators, which are path-illegal unescaped.
        path = f"/nodes/{node}/tasks/{quote(upid, safe='')}/status"
        deadline = time.monotonic() + timeout_s
        while True:
            result = await self.read(path)
            data = result.get("data", result)
            if isinstance(data, dict) and data.get("status") == "stopped":
                exitstatus = data.get("exitstatus")
                if exitstatus != "OK":
                    raise ProxmoxError(
                        method="GET",
                        path=path,
                        status_code=0,
                        body=f"PVE task {upid} finished with exitstatus {exitstatus!r}",
                    )
                return data
            if time.monotonic() >= deadline:
                raise ProxmoxError(
                    method="GET",
                    path=path,
                    status_code=0,
                    body=f"PVE task {upid} did not finish within {timeout_s}s",
                )
            await asyncio.sleep(poll_interval)

    async def stop_task(self, node: str, upid: str) -> dict[str, Any]:
        """Ask PVE to stop an in-flight task (#452). Best-effort by nature.

        A DELETE on a task that already finished is not distinguishable here
        from a real failure, and either way the caller's next step is the same -
        unwind whatever the task did - so callers treat any exception raised
        from here as "could not stop it".
        """
        # A UPID contains ':' separators, which are path-illegal unescaped.
        return await self.call("DELETE", f"/nodes/{node}/tasks/{quote(upid, safe='')}")

    async def delete_vm(self, node: str, vmid: int) -> str:
        """Destroy a guest, disks and all. Returns the UPID of the destroy task.

        `purge` drops the VM from the jobs/pools/HA entries that reference it and
        `destroy-unreferenced-disks` takes the disks PVE would otherwise leave on
        storage - without both, unwinding a half-created guest leaves exactly the
        debris the cancel was meant to remove.
        """
        result = await self.call(
            "DELETE",
            f"/nodes/{node}/qemu/{vmid}",
            query={"purge": 1, "destroy-unreferenced-disks": 1},
        )
        return str(result.get("data", ""))

    async def set_vm_config(self, node: str, vmid: int, config: dict[str, Any]) -> dict[str, Any]:
        body = dict(config)
        # PVE declares the cloud-init `sshkeys` property with format 'urlencoded'
        # and uri_unescape()s the stored value once, server-side, whatever the
        # request transport was. We send a JSON body, so no transport layer adds
        # an encoding of its own: exactly ONE quote() here is what the API
        # requires. The "double URL-encode sshkeys" folklore describes
        # form-encoded/query transports (pvesh, curl -d), where the transport
        # supplies the second layer — encoding twice over JSON would store a
        # literal '%2B'-riddled key and lock the user out.
        if body.get("sshkeys"):
            body["sshkeys"] = quote(str(body["sshkeys"]), safe="")
        return await self.call("POST", f"/nodes/{node}/qemu/{vmid}/config", body=body)

    async def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> dict[str, Any]:
        return await self.call(
            "PUT",
            f"/nodes/{node}/qemu/{vmid}/resize",
            body={"disk": disk, "size": size},
        )

    async def start_vm(self, node: str, vmid: int) -> str:
        result = await self.call("POST", f"/nodes/{node}/qemu/{vmid}/status/start")
        return str(result.get("data", ""))

    async def stop_vm(self, node: str, vmid: int) -> str:
        # Hard stop (power button). The guest API deliberately offers this and
        # shutdown-by-reboot only - a wedged guest OS must not be able to make
        # its own machine unstoppable.
        result = await self.call("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")
        return str(result.get("data", ""))

    async def reboot_vm(self, node: str, vmid: int) -> str:
        result = await self.call("POST", f"/nodes/{node}/qemu/{vmid}/status/reboot")
        return str(result.get("data", ""))

    async def get_vm_current(self, node: str, vmid: int) -> dict[str, Any]:
        return await self.read(f"/nodes/{node}/qemu/{vmid}/status/current")

    async def get_vm_agent_network(self, node: str, vmid: int) -> dict[str, Any] | None:
        """Guest-agent interface list, or None when the agent cannot answer.

        A missing/not-yet-started qemu-guest-agent is the normal case for a fresh
        clone, not an error — callers treat None as "no IP known yet".
        """
        try:
            return await self.read(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        except ProxmoxError:
            return None

    async def agent_ping(self, node: str, vmid: int) -> bool:
        """True only when qemu-guest-agent actually answers inside the guest.

        The cheapest honest precondition for "HomePilot can run something in
        here": a guest with the Agent option enabled but no running qemu-guest-agent
        accepts the config and answers nothing.
        """
        try:
            await self.call("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping")
            return True
        except ProxmoxError:
            return False

    async def agent_exec(
        self, node: str, vmid: int, command: list[str], capture_output: bool = False
    ) -> dict[str, Any]:
        """Run one command inside a guest through qemu-guest-agent.

        PVE takes the argv as repeated `command` parameters; a single string
        would be handed to the guest as one argv[0] and fail. Raises ProxmoxError
        when the agent is absent or the call is refused — callers decide whether
        that is fatal.

        Returns the guest agent's PID, NOT a result: exec is asynchronous, so a
        caller that needs the outcome must poll `agent_exec_status`.
        `capture_output` is what makes that outcome carry stdout/stderr.
        """
        body: dict[str, Any] = {"command": command}
        if capture_output:
            body["capture-output"] = 1
        return await self.call(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            body=body,
        )

    async def agent_exec_status(self, node: str, vmid: int, pid: int) -> dict[str, Any]:
        """Status of a command started by `agent_exec`.

        Carries `exited`, `exitcode`, and (when the exec captured output)
        `out-data`/`err-data`.
        """
        return await self.read(
            f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            query={"pid": pid},
        )

    @staticmethod
    def exec_pid(payload: dict[str, Any] | None) -> int | None:
        """The pid inside an agent-exec answer, or None if it carried none."""
        if not payload:
            return None
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        if pid is None:
            return None
        try:
            return int(pid)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _exec_text(value: Any) -> str:
        """Guest output as text.

        PVE hands stdout back as a string, but a guest agent that returns bytes
        must not turn into an unreadable error message.
        """
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    async def agent_run(
        self,
        node: str,
        vmid: int,
        script: str,
        timeout_s: float = 300.0,
        poll_interval: float = 2.0,
    ) -> tuple[int, str, str]:
        """Run a shell script in a guest and WAIT for it, returning (rc, out, err).

        `agent_exec` is fire-and-forget: it answers with a pid, not a result. A
        caller that does not poll exec-status has no idea whether the command
        ran, let alone whether it worked - the tailnet join reported "joined"
        off the pid alone, so a `tailscale up` that failed was recorded as a
        success (#628). The same mistake as a PVE UPID, one layer down.

        Raises ProxmoxError if the agent refuses the command, RuntimeError if it
        accepts it without a pid, and TimeoutError if it never exits.
        """
        started = await self.agent_exec(node, vmid, ["sh", "-c", script], capture_output=True)
        # Reached on the CLASS, not through self: these two are fixed rules for
        # reading a PVE answer, not behaviour a subclass or a test double should
        # be able to replace out from under the wait loop.
        pid = ProxmoxClient.exec_pid(started)
        if pid is None:
            raise RuntimeError("the guest agent accepted the command but returned no pid")
        deadline = time.monotonic() + timeout_s
        while True:
            status = await self.agent_exec_status(node, vmid, pid)
            data = status.get("data", status)
            if isinstance(data, dict) and data.get("exited"):
                return (
                    int(data.get("exitcode") or 0),
                    ProxmoxClient._exec_text(data.get("out-data")),
                    ProxmoxClient._exec_text(data.get("err-data")),
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"the guest command did not finish within {timeout_s:.0f}s")
            await asyncio.sleep(poll_interval)

    async def agent_write_file(
        self, node: str, vmid: int, path: str, content: str
    ) -> dict[str, Any]:
        """Write a file inside a guest through qemu-guest-agent.

        The way to hand a secret to a guest: an argv is visible in the guest's
        process list and is echoed back in PVE task errors, while a file the
        caller deletes immediately is not.
        """
        return await self.call(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/file-write",
            body={"file": path, "content": content},
        )

    async def next_vmid(self, node: str) -> int:
        result = await self.read("/cluster/nextid")
        return int(result.get("data", result))

    async def cluster_vmids(self) -> set[int]:
        """Every VMID the cluster currently holds — guests AND templates.

        Cluster-wide rather than per node on purpose: a VMID is unique across
        the whole cluster, so a per-node listing would call an id free that PVE
        then refuses. Used to REFUSE a template build onto an id that is already
        taken, which is the one guard between "create a template" and
        "overwrite somebody's VM".
        """
        result = await self.read("/cluster/resources", query={"type": "vm"})
        rows = result.get("data", result)
        out: set[int] = set()
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or row.get("vmid") is None:
                    continue
                with contextlib.suppress(TypeError, ValueError):
                    out.add(int(row["vmid"]))
        return out

    async def get_storage(self, storage: str) -> dict[str, Any]:
        """One storage's cluster-level definition (type, `content` list, path)."""
        result = await self.read(f"/storage/{storage}")
        data = result.get("data", result)
        return data if isinstance(data, dict) else {}

    async def set_storage_content(self, storage: str, content: str) -> dict[str, Any]:
        """REPLACE a storage's content-type list with `content` (a CSV).

        PVE takes the whole list, never a delta, so a caller that means "add one
        type" must send the existing ones back with it — dropping them would
        un-declare content the storage is already holding.
        """
        return await self.call("PUT", f"/storage/{storage}", body={"content": content})

    async def download_url_to_storage(
        self, node: str, storage: str, url: str, filename: str, content: str = "import"
    ) -> str:
        """Have the NODE fetch a file straight onto a storage. Returns the UPID.

        The endpoint accepts `iso`, `vztmpl` and `import` content only; a cloud
        image (qcow2) is `import` content, which is what makes it usable as an
        `import-from` source without root on the node.
        """
        result = await self.call(
            "POST",
            f"/nodes/{node}/storage/{storage}/download-url",
            body={"content": content, "filename": filename, "url": url},
        )
        return str(result.get("data", "") or "")

    async def create_vm(self, node: str, vmid: int, config: dict[str, Any]) -> str:
        """Create an empty guest shell at `vmid`. Returns the UPID, if PVE gives one.

        NOT a clone: this is the first half of building a cloud-init TEMPLATE,
        where there is nothing to clone from yet.
        """
        result = await self.call("POST", f"/nodes/{node}/qemu", body={"vmid": vmid, **config})
        return str(result.get("data", "") or "")

    async def convert_vm_to_template(self, node: str, vmid: int) -> str:
        """Turn a stopped guest into a template. Returns the UPID, if any.

        One-way: PVE has no un-template call, which is why the caller must be
        sure of the vmid before it gets here.
        """
        result = await self.call("POST", f"/nodes/{node}/qemu/{vmid}/template")
        return str(result.get("data", "") or "")

    async def test_connection(self) -> bool:
        try:
            await self.read("/version")
            return True
        except ProxmoxError:
            return False

    async def get_node_status(self, node: str) -> dict[str, Any]:
        return await self.read(f"/nodes/{node}/status")

    async def close(self) -> None:
        await self._client.aclose()
        if self._has_separate_write:
            await self._write_client.aclose()
