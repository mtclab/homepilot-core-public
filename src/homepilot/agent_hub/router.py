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


def _advertised_hub_host(request: Request, bind_host: str) -> str:
    """The host an agent should dial to reach the hub.

    The hub's configured host is usually a bind address (0.0.0.0 / ::) which is
    not routable. The operator reaches this API at a real address, so fall back
    to the request's hostname for the install instructions.
    """
    # The "0.0.0.0" literal here is a comparison, not a socket bind.
    if bind_host and bind_host not in ("0.0.0.0", "::", ""):  # nosec B104
        return bind_host
    return request.url.hostname or bind_host


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
async def list_agents() -> list[dict[str, Any]]:
    registry = _get_registry()
    return registry.list_connected()


@router.get("/token", dependencies=[_admin_only])
async def get_hub_token(request: Request) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    token = hub.auth_token or ""
    return {
        "auth_token": token,
        "hub_host": _advertised_hub_host(request, hub.host),
        "hub_port": hub.port,
    }


@router.get("/bootstrap", dependencies=[_admin_only])
async def create_bootstrap_token(request: Request) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    token = await hub._token_store.create()
    return {
        "bootstrap_token": token,
        "hub_host": _advertised_hub_host(request, hub.host),
        "hub_port": hub.port,
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
