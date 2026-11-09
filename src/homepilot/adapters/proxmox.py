from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal as _signal
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse

import httpx

logger = logging.getLogger(__name__)

# The two collections PVE files guests under. A VMID alone does not say which
# one a guest lives in, so every by-vmid path has to resolve the type first.
GUEST_TYPES = ("qemu", "lxc")

# Artifact target kinds that name a guest, mapped onto the PVE collection. PVE
# spellings map to themselves so a caller may pass either vocabulary.
TARGET_KIND_GUEST_TYPES = {"vm": "qemu", "qemu": "qemu", "lxc": "lxc"}

# The power actions PVE offers both collections under `status/<action>` (#781).
GUEST_POWER_ACTIONS = ("start", "shutdown", "reboot", "stop")


# PVE's own verdict for a task that SUCCEEDED but logged warnings: pve-common's
# upid_status_is_error() treats exactly "OK" and "WARNINGS: <n>" as success. A
# VM start that warned about its config, a snapshot that warned about a volume -
# those guests are up and those snapshots exist; failing the run over it
# destroyed a working guest.
_WARNINGS_RE = re.compile(r"^WARNINGS: (\d+)$")


def task_warnings(exitstatus: Any) -> int | None:
    """How many warnings a finished PVE task logged, or None if it did not succeed.

    0 for a clean "OK", n for "WARNINGS: n", None for anything else - which is
    a failure.
    """
    if exitstatus == "OK":
        return 0
    match = _WARNINGS_RE.match(str(exitstatus or ""))
    if match is None:
        return None
    return int(match.group(1))


def _query_value(value: Any) -> Any:
    """A parameter as PVE reads it from a query string: booleans are 1/0.

    A JSON body may carry ``true``; a URL carries text, and httpx would spell a
    Python bool ``true``, which PVE's boolean schema does not take."""
    if isinstance(value, bool):
        return int(value)
    return value


def wire_parameters(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    query: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Where each parameter of a PVE call must travel: ``(path, body, query)``.

    Two rules of the wire, both of which lost parameters before:

    * PVE reads a DELETE's parameters from the URL query only. A DELETE sent
      with a JSON body is refused, ``501 Unexpected content for method
      'DELETE'`` (#797) - an approved destroy step with ``body: {purge: 1}``
      failed at apply, after the steps before it had already run. So a DELETE's
      body is sent as query parameters, which keeps every artifact written with
      ``body:`` on a DELETE step working rather than refusing it.
    * httpx REPLACES a URL's own query string when ``params`` is given. A path
      carrying ``?purge=1`` plus a ``query`` dict silently dropped ``purge``.
      The path's own parameters are folded into the dict instead (the dict wins
      on a clash, being the explicit one).
    """
    if method.upper() == "DELETE" and body:
        query = {**body, **(query or {})}
        body = None
    if query is None:
        return path, body, None
    merged: dict[str, Any] = {}
    if "?" in path:
        path, own = path.split("?", 1)
        merged.update(parse_qsl(own, keep_blank_values=True))
    merged.update(query)
    return path, body, {k: _query_value(v) for k, v in merged.items()}


def _signal_name(signum: int) -> str:
    try:
        return _signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


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


class ProxmoxTaskStillRunningError(ProxmoxError):
    """The wait on a PVE task ran out while PVE still reports it RUNNING (#774).

    That is not a failure: a vzdump of three guests took 13 minutes, outlived a
    600 s wait, was reported failed - and finished `OK` with every archive on
    disk. The outcome of a task we stopped watching is UNKNOWN, and a caller
    must say so rather than pick a side. It stays a ProxmoxError so every caller
    that does not yet tell the two apart keeps treating it as "not succeeded",
    never as success.
    """

    def __init__(self, path: str, node: str, upid: str, waited_s: float):
        self.node = node
        self.upid = upid
        self.waited_s = waited_s
        super().__init__(
            method="GET",
            path=path,
            status_code=0,
            body=f"PVE task {upid} did not finish within {waited_s}s and is still running",
        )


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
        # The same rule the resolver and the SDN/firewall gateway apply (#715):
        # a write "token" that is not a PVE token is never sent. It used to be
        # sent for every clone/destroy/config write (401) while the gateway
        # quietly used the read token - so SDN writes worked and guest writes
        # failed, off one stored value.
        from ..proxmox_config import is_pve_token

        if write_token and not is_pve_token(write_token):
            logger.warning(
                "The configured PVE write token is not a token - using the read token "
                "for writes; re-save Proxmox settings to repair it"
            )
            write_token = None
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
        path, body, query = wire_parameters(method_upper, path, body, query)
        is_mutation = method_upper in ("POST", "PUT", "PATCH", "DELETE")
        client = self._write_client if is_mutation else self._client
        if is_mutation:
            # THE wire for every HomePilot write to PVE, so the firewall
            # lockout guard runs here and nowhere upstream (#707): an artifact
            # step, a provisioning write and anything added later all pass it.
            await self._guard_firewall_options_write(method_upper, path, body, query)
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

    async def _guard_firewall_options_write(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        query: dict[str, Any] | None,
    ) -> None:
        """Refuse, before sending, a firewall-options write that could lock out management.

        #600's guard covered the guest-network path only; a proxmox-api-sequence
        step ``PUT /cluster/firewall/options {enable: 1}`` reached the wire with
        PVE's default DROP input policy - the #600 lockout, through another door
        (#707). Every parameter source PVE reads is merged (the path's own query
        string, ``query``, the body) so none of them is a way round it.
        """
        from .pve_firewall_guard import (
            PveFirewallLockoutError,
            check_firewall_options_write,
            firewall_options_scope,
            management_source,
            query_params,
        )

        try:
            scope = firewall_options_scope(path)
            if scope is None:
                return
            write = {**query_params(path), **(query or {}), **(body or {})}
            await check_firewall_options_write(
                scope,
                write,
                _ClientFirewallReader(self),
                ports=(22, self.api_port),
                source=await management_source(self.api_host, self.api_port),
            )
        except PveFirewallLockoutError as exc:
            raise ProxmoxError(
                method=method,
                path=path,
                status_code=0,
                body=f"REFUSED by HomePilot before sending: {exc}",
                hint="firewall lockout guard (#600, #707)",
            ) from exc

    @property
    def api_host(self) -> str | None:
        """The host this client talks to, as configured (a name or an address).

        Read by the fence verification: the Proxmox host is the one address
        HomePilot KNOWS is alive when a guest has just been cloned through it.
        """
        parsed = urlparse(self._base_url if "://" in self._base_url else f"//{self._base_url}")
        return parsed.hostname or None

    @property
    def api_port(self) -> int:
        parsed = urlparse(self._base_url if "://" in self._base_url else f"//{self._base_url}")
        return parsed.port or 8006

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
        description: str | None = None,
    ) -> str:
        """Clone a template into a new VM. Returns the UPID of the clone task.

        ``description`` is written into the new guest's config by the clone
        itself. Provisioning puts its own run marker there (#719), so "did THIS
        run create the VM at that vmid" can be answered from the cluster rather
        than assumed from the fact that a request was sent.

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
        if description:
            body["description"] = description
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

        Raises ProxmoxError if the task ends in failure, or if the timeout
        elapses first - a caller must never mistake "still running" or "failed"
        for success. Success is what PVE itself calls success: "OK", and
        "WARNINGS: <n>" (#711). The returned dict carries `warnings` (the count)
        so a caller can put it on its record.
        """
        path = f"/nodes/{node}/tasks/{quote(upid, safe='')}/status"
        data = await self.wait_until_stopped(node, upid, timeout_s, poll_interval)
        exitstatus = data.get("exitstatus")
        warnings = task_warnings(exitstatus)
        if warnings is None:
            raise ProxmoxError(
                method="GET",
                path=path,
                status_code=0,
                body=f"PVE task {upid} finished with exitstatus {exitstatus!r}",
            )
        if warnings:
            logger.warning("PVE task %s succeeded with %d warning(s)", upid, warnings)
        return {**data, "warnings": warnings}

    async def task_warning_lines(self, node: str, upid: str, limit: int = 2000) -> list[str]:
        """The warning lines a finished PVE task wrote to its log.

        A task that ends ``WARNINGS: n`` succeeded (#711), and the count alone
        says nothing an operator can judge - is it the harmless UEFI note or a
        disk that did not make it into the backup? PVE writes each warning as a
        ``WARN: ...`` line in the task log; those are what this returns, in
        order, without the prefix. Read errors propagate (ProxmoxError): the
        caller decides what a log it could not read means.
        """
        path = f"/nodes/{node}/tasks/{quote(upid, safe='')}/log"
        result = await self.read(path, query={"start": 0, "limit": limit})
        rows = result.get("data", result)
        lines: list[str] = []
        if isinstance(rows, list):
            for row in rows:
                text = str(row.get("t", "")) if isinstance(row, dict) else str(row)
                stripped = text.strip()
                if stripped.upper().startswith("WARN:"):
                    lines.append(stripped[5:].strip())
                elif stripped.upper().startswith("WARNING:"):
                    lines.append(stripped[8:].strip())
        return lines

    async def wait_until_stopped(
        self,
        node: str,
        upid: str,
        timeout_s: float = 600.0,
        poll_interval: float = 2.0,
    ) -> dict[str, Any]:
        """Block until a PVE task has STOPPED, whatever its outcome; return its status.

        For the unwind: after `stop_task`, the thing to know is that the task no
        longer holds the guest's lock, not whether it "succeeded" - a task that
        was stopped ends with an error status by definition. Raises ProxmoxError
        only when the task is still running at the deadline.
        """
        # A UPID contains ':' separators, which are path-illegal unescaped.
        path = f"/nodes/{node}/tasks/{quote(upid, safe='')}/status"
        deadline = time.monotonic() + timeout_s
        while True:
            result = await self.read(path)
            data = result.get("data", result)
            if isinstance(data, dict) and data.get("status") == "stopped":
                return data
            if time.monotonic() >= deadline:
                # Still running is not failed (#774): the caller decides what a
                # task it stopped watching means, and it must be able to tell.
                raise ProxmoxTaskStillRunningError(path, node, upid, timeout_s)
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

    async def get_vm_config(
        self, node: str, vmid: int, guest_type: str = "qemu", *, current: bool = False
    ) -> dict[str, Any]:
        """One guest's configuration as PVE stores it (#630).

        `guest_type` because an LXC container's config lives in a different
        collection and states its address on the NIC line rather than on a
        cloud-init one - and a container on the guest wire holds an address
        just as firmly as a VM does.

        `current`: PVE answers a plain GET with the PENDING changes already
        applied - the config the guest will boot with next, not the one it is
        running. ``current=True`` asks for what is really attached now (#781).
        """
        collection = "lxc" if guest_type == "lxc" else "qemu"
        query = {"current": 1} if current else None
        result = await self.read(f"/nodes/{node}/{collection}/{vmid}/config", query)
        data = result.get("data", result)
        return data if isinstance(data, dict) else {}

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

    async def power_guest(
        self,
        node: str,
        vmid: int,
        action: str,
        *,
        guest_type: str = "qemu",
        timeout_s: int | None = None,
    ) -> str | None:
        """Send one power action to a VM or a container; the task's UPID, or None.

        The operator power controls (#781), for both collections. ``shutdown``
        is sent with ``forceStop: 0`` ALWAYS: a guest that does not shut down
        within ``timeout_s`` stays running and its task fails, so the caller
        can say "timed out" - never a silent escalation to a hard stop.
        ``reboot`` is PVE's own (shutdown, then start the guest again), which
        is what applies pending config such as a cloud-init ``ipconfigN``.
        """
        if action not in GUEST_POWER_ACTIONS:
            raise ValueError(f"unknown power action {action!r}")
        collection = "lxc" if guest_type == "lxc" else "qemu"
        body: dict[str, Any] = {}
        if action in ("shutdown", "reboot") and timeout_s is not None:
            body["timeout"] = int(timeout_s)
        if action == "shutdown":
            body["forceStop"] = 0
        result = await self.call(
            "POST", f"/nodes/{node}/{collection}/{vmid}/status/{action}", body=body or None
        )
        return self.upid_of(result)

    async def get_guest_status(
        self, node: str, vmid: int, guest_type: str = "qemu"
    ) -> dict[str, Any]:
        """A VM's or container's live status (``status``, ``name``, ``lock``...)."""
        collection = "lxc" if guest_type == "lxc" else "qemu"
        result = await self.read(f"/nodes/{node}/{collection}/{vmid}/status/current")
        data = result.get("data", result)
        return data if isinstance(data, dict) else {}

    async def get_guest_pending(
        self, node: str, vmid: int, guest_type: str = "qemu"
    ) -> list[dict[str, Any]]:
        """PVE's ``/pending`` rows: ``{key, value[, pending][, delete]}`` per config key."""
        collection = "lxc" if guest_type == "lxc" else "qemu"
        result = await self.read(f"/nodes/{node}/{collection}/{vmid}/pending")
        data = result.get("data", result)
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

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

    async def agent_exec(self, node: str, vmid: int, command: list[str]) -> dict[str, Any]:
        """Run one command inside a guest through qemu-guest-agent.

        PVE takes the argv as repeated `command` parameters; a single string
        would be handed to the guest as one argv[0] and fail. Raises ProxmoxError
        when the agent is absent or the call is refused — callers decide whether
        that is fatal.

        Returns the guest agent's PID, NOT a result: exec is asynchronous, so a
        caller that needs the outcome must poll `agent_exec_status`, which does
        carry the guest's stdout and stderr.

        `command` IS THE ONLY PARAMETER. 3.6.12 also sent `capture-output: 1`,
        reasoning that output had to be asked for - and PVE refused every call
        with `400 property is not defined in schema and the schema does not
        allow additional properties`, because its exec endpoint declares
        `additionalProperties => 0` and has never taken that argument. PVE sets
        `capture-output` on the guest-agent command ITSELF, so exec-status
        carries out-data/err-data either way.

        That one invented parameter is the whole of #628's live failure: it made
        `agent_run` - the wait-and-check-the-exit-status loop that the tailnet
        join was rewritten around - fail on its FIRST call against every real
        guest, which the operator saw as "Could not ask vmid 101 whether
        tailscale is installed". It shipped because every fake in the suite
        answered exec with a pid no matter what was sent.
        """
        return await self.call(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            body={"command": command},
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
        started = await self.agent_exec(node, vmid, ["sh", "-c", script])
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
                return ProxmoxClient._exec_result(data)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"the guest command did not finish within {timeout_s:.0f}s")
            await asyncio.sleep(poll_interval)

    @staticmethod
    def _exec_result(data: dict[str, Any]) -> tuple[int, str, str]:
        """(rc, out, err) for a finished guest-exec, honest about how it ended.

        qemu-guest-agent reports a process that EXITED with `exitcode`, and one
        that was KILLED with `signal` and no exitcode at all. Reading a missing
        exitcode as 0 turned an OOM-killed `tailscale up` into "joined" (#711).
        A killed process gets the shell's own convention, 128 + signal, and the
        signal is named in `err`; one that reports neither is not a success
        either, because nothing says it succeeded.
        """
        out = ProxmoxClient._exec_text(data.get("out-data"))
        err = ProxmoxClient._exec_text(data.get("err-data"))
        signum = data.get("signal")
        if signum is not None:
            with contextlib.suppress(TypeError, ValueError):
                number = int(signum)
                said = f"the command was killed by {_signal_name(number)} ({number})"
                return 128 + number, out, f"{err}\n{said}".strip()
        exitcode = data.get("exitcode")
        if exitcode is None:
            said = "the guest agent reported the command finished but gave no exit code"
            return 255, out, f"{err}\n{said}".strip()
        try:
            return int(exitcode), out, err
        except (TypeError, ValueError):
            return 255, out, f"{err}\nthe guest agent reported exit code {exitcode!r}".strip()

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

    async def check_tokens(self) -> dict[str, dict[str, Any]]:
        """Does EACH configured token actually authenticate? (#624)

        `test_connection` only ever exercised the read client, so a stale or
        rotated WRITE token was invisible until the first mutating call - and on
        prod, 2026-08-28, that call was a friend's first invite redemption:
        `write_token_configured: true`, `connection_status: ok`, and every
        clone 401ing in their face. "Configured" is not "authenticates", which
        is the same distinction the artifacts remote had to learn.

        Each token is checked with the same harmless authenticated read, sent
        through ITS OWN client so the credential under test is the one on the
        wire. Bounded honestly: this proves the credential AUTHENTICATES. It
        does not prove it carries the privileges a clone needs, because the only
        way to establish that is to mutate something, and a health check may not.
        """
        out: dict[str, dict[str, Any]] = {}

        async def _probe(client: httpx.AsyncClient) -> dict[str, Any]:
            try:
                response = await client.request(method="GET", url="/version")
                response.raise_for_status()
                return {"ok": True, "status": 0, "detail": ""}
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                return {
                    "ok": False,
                    "status": code,
                    "detail": (
                        "the token was refused (401)"
                        if code == 401
                        else f"the cluster answered {code}"
                    ),
                }
            except httpx.RequestError as exc:
                return {"ok": False, "status": 0, "detail": f"the cluster is unreachable: {exc}"}

        # CONCURRENTLY. Two sequential probes doubled the work inside the
        # self-check's 2-second budget and pushed the Proxmox subsystem from
        # `ok` to `unknown` on a real cluster - a check that times out is a
        # check that establishes nothing, which is the opposite of the point
        # (caught driving 3.6.19 on dev, not by the suite).
        if self._has_separate_write:
            out["read"], out["write"] = await asyncio.gather(
                _probe(self._client), _probe(self._write_client)
            )
        else:
            out["read"] = await _probe(self._client)
            # No separate credential: the read token IS the write token, so its
            # verdict is the whole answer and inventing a second one would imply
            # a check that never happened.
            out["write"] = dict(out["read"], detail="the read token is used for writes too")
        return out

    async def get_node_status(self, node: str) -> dict[str, Any]:
        return await self.read(f"/nodes/{node}/status")

    async def close(self) -> None:
        await self._client.aclose()
        if self._has_separate_write:
            await self._write_client.aclose()


def _pve_data(payload: Any) -> Any:
    return payload.get("data") if isinstance(payload, dict) else payload


class _ClientFirewallReader:
    """The lockout guard's reads, made with the same client that is about to write."""

    def __init__(self, client: ProxmoxClient) -> None:
        self._client = client

    async def cluster_firewall_options(self) -> dict[str, Any]:
        data = _pve_data(await self._client.read("cluster/firewall/options"))
        return data if isinstance(data, dict) else {}

    async def cluster_firewall_rules(self) -> list[dict[str, Any]]:
        data = _pve_data(await self._client.read("cluster/firewall/rules"))
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def node_firewall_options(self, node: str) -> dict[str, Any]:
        data = _pve_data(await self._client.read(f"nodes/{quote(node, safe='')}/firewall/options"))
        return data if isinstance(data, dict) else {}

    async def node_firewall_rules(self, node: str) -> list[dict[str, Any]]:
        data = _pve_data(await self._client.read(f"nodes/{quote(node, safe='')}/firewall/rules"))
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def node_names(self) -> list[str]:
        data = _pve_data(await self._client.read("nodes"))
        if not isinstance(data, list):
            raise ProxmoxError("GET", "nodes", 0, "the node list was not a list")
        return [str(r["node"]) for r in data if isinstance(r, dict) and r.get("node")]
