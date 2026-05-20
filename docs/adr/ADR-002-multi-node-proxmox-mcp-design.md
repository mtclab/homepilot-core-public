# ADR-002: Multi-Node Proxmox MCP Design (MultiClient Facade)

**Status:** Accepted  
**Date:** 2026-05-17  
**Deciders:** Architect, InfraOps, Integrator

## Context

The original `proxmox-mcp` server supported a single Proxmox VE endpoint via one `ProxmoxClient` instance connected to one PVE node. As HomePilot's infrastructure grows to multiple PVE clusters (dev, staging, production) and potentially multi-node clusters, the MCP server needed to:

1. Connect to multiple PVE endpoints simultaneously
2. Route operations to the correct endpoint based on guest/node location
3. Fail over between endpoints if one becomes unavailable
4. Present a unified API to LLM consumers regardless of backend topology

The naive approach would be N parallel MCP server instances (one per endpoint), but this would:
- Require LLM consumers to manage N connections
- Make cross-cluster operations (e.g., migrate between clusters) impossible
- Duplicate tool definitions N times in the MCP tool registry

## Decision

We implemented a **MultiClient facade pattern** (`multi_client.py`) that:

1. **Single entry point**: One `MultiClient` instance holds connections to N PVE endpoints, each with its own monitor + admin token pair
2. **Endpoint routing**: Every tool accepts an optional `endpoint` parameter. If omitted, operations route to the default endpoint. If specified, they route to the named endpoint
3. **Guest resolution**: `resolve_guest(vmid)` checks guest location across all endpoints and returns `ResolvedGuest(endpoint, node, vmid)`, enabling automatic routing
4. **Node resolution**: `resolve_node(node)` finds which endpoint owns a node name via `resolve_node()` that queries all endpoints
5. **Failover**: If the default endpoint is unreachable, operations automatically try other endpoints for read-only queries
6. **Configuration**: Endpoints are defined in `.env` as `PROXMOX_ENDPOINTS=host1:8006,host2:8006` with separate token pairs per endpoint

### Architecture

```
┌─────────────────────────────────────────────┐
│             MCP Tool Layer                    │
│  (proxmox_create_vm, proxmox_list_vms, ...)  │
│         endpoint param on every tool          │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│           MultiClient Facade                  │
│                                              │
│  default_endpoint ──► Client A (pve-dev)     │
│  endpoints[1]     ──► Client B (pve-prod)    │
│  endpoints[2]     ──► Client C (pve-staging) │
│                                              │
│  resolve_guest(vmid) → query all endpoints    │
│  resolve_node(node)  → query all endpoints    │
└──────────────────────────────────────────────┘
```

### Key Implementation Details

- `MultiClient.get_client(elevated, endpoint)` returns the appropriate `ProxmoxAPI` instance
- `safe_api_call()` wraps every PVE call with retry, error handling, and endpoint-aware error messages
- `_api(client)` and `_elevated(client)` shorthand functions pass the correct client
- Every `lifecycle.py`/`discovery.py`/etc function signature includes `endpoint: str | None = None`

## Consequences

### Positive
- Single MCP connection serves all PVE clusters — LLM consumers see one unified API
- Cross-cluster operations are possible (remote migration, cross-cluster backup restore)
- Automatic guest/node resolution removes need for users to know which endpoint owns which resource
- Adding a new PVE endpoint requires only `.env` configuration change, no code changes
- Read-only failover increases resilience for monitoring queries

### Negative
- All endpoints' admin tokens are in one process — compromise exposes all clusters
- Slightly higher latency for `resolve_guest()` calls that must query multiple endpoints
- Error messages must disambiguate which endpoint failed
- Configuration is more complex (N token pairs to manage)

### Risks
- **Token sprawl**: Each endpoint needs separate monitor + admin tokens. Must rotate all tokens together. Mitigated by vault integration (ADR-001).
- **State inconsistency**: If a guest migrates between endpoints, cached resolution may be stale. Mitigated by re-querying on 404 errors.
- **Single-process bottleneck**: One MCP server process handles all endpoints. Mitigated by async I/O and per-endpoint connection pools.