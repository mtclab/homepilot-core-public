from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..auth.deps import require_scope
from .registry import AgentRegistry

if TYPE_CHECKING:
    from ..adapters.agent import AgentAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

_admin_only = Depends(require_scope("admin"))


def _advertised_hub(request: Request, bind_host: str, bind_port: int) -> tuple[str, int]:
    """The host:port an agent should dial to reach the hub.

    Resolution order:
      1. HP_AGENT_HUB_ADVERTISE_HOST (operator-set; may be "host" or "host:port") —
         the only reliable source behind a reverse proxy.
      2. the configured bind host, if it is not a wildcard.
      3. the request hostname (the address the operator reached the API on).
    """
    from homepilot.config import get_settings

    advertise = (get_settings().agent_hub_advertise_host or "").strip()
    if advertise:
        advertise = advertise.removeprefix("http://").removeprefix("https://")
        if ":" in advertise:
            host, _, port = advertise.rpartition(":")
            try:
                return host, int(port)
            except ValueError:
                return advertise, bind_port
        return advertise, bind_port

    # The "0.0.0.0" literal here is a comparison, not a socket bind.
    if bind_host and bind_host not in ("0.0.0.0", "::", ""):  # nosec B104
        return bind_host, bind_port
    return (request.url.hostname or bind_host), bind_port


def _get_registry() -> AgentRegistry:
    from homepilot.app_state import get_agent_registry

    registry = get_agent_registry()
    if registry is None:
        raise HTTPException(status_code=503, detail="Agent hub not enabled")
    return registry


def _get_agent_adapter() -> AgentAdapter:
    from homepilot.adapters.agent import AgentAdapter
    from homepilot.app_state import get_agent_registry

    registry = get_agent_registry()
    if registry is None:
        raise HTTPException(status_code=503, detail="Agent hub not enabled")
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")

    from ..main import app

    pve_nodes: list[str] = []
    lifecycle = getattr(app.state, "artifact_lifecycle", None) if app else None
    if lifecycle and hasattr(lifecycle, "_pve_nodes_list"):
        pve_nodes = lifecycle._pve_nodes_list or []

    adapter = AgentAdapter(
        hub_server=hub,
        pve_nodes=pve_nodes,
    )
    return adapter


class HostExecRequest(BaseModel):
    host: str
    command: str
    timeout: int = 30


class HostReadFileRequest(BaseModel):
    host: str
    path: str


class HostWriteFileRequest(BaseModel):
    host: str
    path: str
    content: str


@router.post("/host/exec", dependencies=[_admin_only])
async def exec_on_host(body: HostExecRequest) -> dict[str, Any]:
    adapter = _get_agent_adapter()
    try:
        exit_code, stdout, stderr = await adapter.exec(
            body.host,
            body.command,
            timeout=body.timeout,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


@router.post("/host/read-file", dependencies=[_admin_only])
async def read_file_from_host(body: HostReadFileRequest) -> dict[str, Any]:
    adapter = _get_agent_adapter()
    try:
        content = await adapter.read_file(body.host, body.path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"content": content}


@router.post("/host/write-file", dependencies=[_admin_only])
async def write_file_to_host(body: HostWriteFileRequest) -> dict[str, Any]:
    adapter = _get_agent_adapter()
    try:
        result = await adapter.write_file(body.host, body.path, body.content)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return result


@router.get("/test/adapter", dependencies=[_admin_only])
async def test_adapter() -> dict[str, Any]:
    from homepilot.adapters.agent import AgentAdapterError

    registry = _get_registry()
    connected = registry.list_connected()
    agent_host = connected[0]["hostname"] if connected else None

    adapter = _get_agent_adapter()
    results: dict[str, Any] = {}

    if agent_host:
        try:
            exit_code, stdout, stderr = await adapter.exec(agent_host, "hostname", timeout=10)
            results["agent_connected"] = {
                "host": agent_host,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "status": "ok",
            }
        except AgentAdapterError as exc:
            results["agent_connected"] = {"host": agent_host, "status": "error", "error": str(exc)}
        except Exception as exc:
            results["agent_connected"] = {"host": agent_host, "status": "error", "error": str(exc)}
    else:
        results["agent_connected"] = {"status": "no_agents_connected"}

    try:
        exit_code, stdout, stderr = await adapter.exec(
            "nonexistent.test.local",
            "hostname",
            timeout=5,
        )
        results["fallback_no_agent"] = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "status": "ok",
        }
    except AgentAdapterError as exc:
        results["fallback_no_agent"] = {"status": "error", "error": str(exc)}
    except Exception as exc:
        results["fallback_no_agent"] = {"status": "error", "error": str(exc)}

    results["adapter_info"] = {
        "hub_available": adapter._hub is not None,
        "pve_nodes": adapter._pve_nodes,
        "connected_agents": len(connected),
    }

    return results


@router.get("/", dependencies=[_admin_only])
async def list_agents(request: Request) -> list[dict[str, Any]]:
    """Live connections overlaid on the persisted registry, so agents that are
    mid-reconnect after a backend restart show as known/disconnected rather than
    vanishing (and inventory coverage doesn't flap to 'uncovered')."""
    registry = _get_registry()
    live = {a["agent_id"]: {**a, "connected": True} for a in registry.list_connected()}

    repo = getattr(request.app.state, "repo", None)
    if repo is not None:
        for row in await repo.list_agents():
            if row["agent_id"] in live:
                continue  # live record is authoritative
            live[row["agent_id"]] = {
                "agent_id": row["agent_id"],
                "hostname": row["hostname"],
                "system_info": row.get("system_info") or {},
                "state": row.get("state") or {},
                "connected_at": row.get("connected_at"),
                "last_heartbeat": row.get("last_heartbeat"),
                "connected": False,
                "disconnected_at": row.get("disconnected_at"),
            }
    return list(live.values())


@router.get("/token", dependencies=[_admin_only])
async def get_hub_token(request: Request) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    token = hub.auth_token or ""
    host, port = _advertised_hub(request, hub.host, hub.port)
    return {
        "auth_token": token,
        "hub_host": host,
        "hub_port": port,
    }


@router.get("/bootstrap", dependencies=[_admin_only])
async def create_bootstrap_token(request: Request) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    token = await hub._token_store.create()
    host, port = _advertised_hub(request, hub.host, hub.port)
    return {
        "bootstrap_token": token,
        "hub_host": host,
        "hub_port": port,
    }


@router.get("/audit", dependencies=[_admin_only])
async def get_audit_log(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
    registry = _get_registry()
    return registry.audit_log.query(limit=limit)


@router.get("/hostname/{hostname}", dependencies=[_admin_only])
async def get_agent_by_hostname(hostname: str) -> dict[str, Any]:
    registry = _get_registry()
    agent = registry.get_by_hostname(hostname)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent for hostname {hostname} not connected")
    return {
        "agent_id": agent.agent_id,
        "hostname": agent.hostname,
        "system_info": agent.system_info,
        "state": agent.state,
        "connected_at": agent.connected_at.isoformat(),
        "last_heartbeat": agent.last_heartbeat.isoformat(),
    }


@router.get("/hostname/{hostname}/connected", dependencies=[_admin_only])
async def is_agent_connected(hostname: str) -> dict[str, Any]:
    registry = _get_registry()
    connected = registry.is_connected(hostname)
    return {"hostname": hostname, "connected": connected}


class ExecRequest(BaseModel):
    command: str
    timeout: int = 30


class WriteFileRequest(BaseModel):
    path: str
    content: str


@router.post("/{agent_id}/exec", dependencies=[_admin_only])
async def exec_on_agent(agent_id: str, body: ExecRequest) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    try:
        result = await hub.send_command(agent_id, body.command, timeout=body.timeout)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Command timed out") from None
    return result


@router.post("/{agent_id}/read-file", dependencies=[_admin_only])
async def read_file_from_agent(agent_id: str, path: str = Query(...)) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    try:
        result = await hub.send_read_file(agent_id, path)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Read timed out") from None
    return result


@router.post("/{agent_id}/write-file", dependencies=[_admin_only])
async def write_file_to_agent(agent_id: str, body: WriteFileRequest) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    try:
        result = await hub.send_write_file(agent_id, body.path, body.content)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Write timed out") from None
    return result


@router.post("/{agent_id}/zabbix-push", dependencies=[_admin_only])
async def trigger_zabbix_push(agent_id: str) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    try:
        result = await hub.send_zabbix_push(agent_id)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Zabbix push timed out") from None
    return result


@router.get("/{agent_id}", dependencies=[_admin_only])
async def get_agent(agent_id: str) -> dict[str, Any]:
    registry = _get_registry()
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return {
        "agent_id": agent.agent_id,
        "hostname": agent.hostname,
        "system_info": agent.system_info,
        "state": agent.state,
        "connected_at": agent.connected_at.isoformat(),
        "last_heartbeat": agent.last_heartbeat.isoformat(),
    }
