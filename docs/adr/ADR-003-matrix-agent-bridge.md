# ADR-003: Matrix Agent Bridge (n8n → LLM → Matrix Response Pattern)

**Status:** Accepted  
**Date:** 2026-05-17  
**Deciders:** Architect, PM, Integrator

## Context

HomePilot's agent system needs real-time communication between multiple AI agents operating on the same infrastructure. The existing options were:

1. **Direct API calls between agents** — requires each agent to know every other agent's endpoint
2. **Shared file-based handoffs** — too slow, no real-time notification
3. **Message queue (RabbitMQ/Redis)** — adds infrastructure dependency, overkill for our scale
4. **Matrix protocol** — federated, E2E encrypted, supports threads/reactions, has mature bot SDKs

We already use Matrix as the communication backbone for agent deliberation (planning, debates, convergence). The agents (Architect, PO, PM, Coder, Reviewer, QA, Security, UX, DevOps, InfraOps, DataEngineer, Integrator) post to Matrix rooms using MCP tools.

However, we also need **n8n workflow triggers** to bridge external events (GitHub webhooks, Proxmox alerts, Zabbix triggers) into the agent system. These incoming events need to:
1. Trigger an LLM analysis
2. Post the LLM's assessment to the appropriate Matrix thread
3. Optionally dispatch a coder agent to handle the issue

## Decision

We implemented a **Matrix Agent Bridge** pattern combining n8n webhooks with LLM processing and Matrix posting:

1. **n8n Webhook Receiver**: Each external event type (GitHub PR, PVE alert, Zabbix trigger) has an n8n webhook that receives the payload
2. **LLM Processing**: n8n calls the OpenCode LLM endpoint with the payload and a system prompt tailored to the event type
3. **Matrix Posting**: The LLM response is posted to the appropriate Matrix room/thread via the Matrix MCP tools
4. **Agent Dispatch**: For events requiring action, the LLM response includes a recommendation that triggers agent dispatch

### Flow

```
External Event          n8n Webhook              LLM Analysis           Matrix Room
─────────────    ──►    ───────────    ──►      ──────────────    ──►  ────────────
GitHub PR opened         /webhook/github          "Analyze PR..."       #public-monitoring
PVE node down            /webhook/pve-alert       "Assess impact..."    #public-monitoring
Zabbix trigger           /webhook/zabbix          "Evaluate alert..."   #public-monitoring
```

### Message Types

The Matrix bridge uses structured message types:
- `position` — Agent stating their position on an issue
- `challenge` — Agent challenging another's position
- `agreement` — Agent agreeing with a position (+1 reaction)
- `converged` — Decision reached (requires quorum)
- `decision` — Final decision recorded in handoff document
- `heads-up` — Informational notification
- `question` / `answer` — Clarification exchanges
- `escalation` — Unresolved issue requiring human intervention

### Thread Model

Each issue/work item gets its own Matrix thread (root event). All agent messages within that issue are posted as replies to the thread root. This keeps conversations organized and enables `matrix_read(thread_root=X)` to retrieve all messages for a specific issue.

## Consequences

### Positive
- Real-time agent communication with persistent, searchable history
- Thread-scoped conversations prevent context pollution between issues
- Structured message types enable automated decision tracking and convergence detection
- n8n provides visual workflow editor for non-technical operators
- Matrix's E2E encryption secures sensitive infrastructure discussions
- Handoff documents (stored in `$HOME/.config/homepilot/handoffs/`) provide permanent record alongside ephemeral Matrix messages

### Negative
- Matrix server dependency — if Matrix is down, agents can't communicate
- n8n dependency — if n8n is down, external events aren't bridged
- Latency: webhook → n8n → LLM → Matrix adds ~5-10 seconds per event
- LLM cost: every external event triggers an LLM call, even false-positive alerts

### Risks
- **Matrix server outage**: Agents fall back to file-based handoffs. Conversation is lost but decisions persist in handoff files.
- **LLM rate limiting**: n8n must handle 429 errors with backoff retry logic.
- **Message ordering**: Matrix doesn't guarantee strict ordering. Agents must include timestamps and sequence numbers for causal ordering.
- **Thread explosion**: Each issue creates a new thread. Mitigated by archiving resolved threads and periodic cleanup.