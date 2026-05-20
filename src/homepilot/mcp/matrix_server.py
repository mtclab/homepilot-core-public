"""HomePilot Matrix MCP Server — agent-callable Matrix room tools.

Separate MCP server for Matrix communication. Registered in opencode.json
as the 'matrix' MCP server. Runs as a stdio subprocess.

Bots: hp-{architect,po,pm,coder,reviewer,qa,security}:example.com
Room: configured via MATRIX_ROOM_ID env or secrets/matrix_room.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any

import aiohttp
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)

_ROLES = ("architect", "po", "pm", "coder", "reviewer", "qa", "security")

_THREAD_ROOT_PREFIX = "🧵 THREAD-ROOT:"

_MSG_TYPE_EMOJI: dict[str, str] = {
    "position": "📋 Position",
    "challenge": "⚔️ Challenge",
    "agreement": "✅ Agreement",
    "converged": "🎯 Converged",
    "decision": "⚡ Decision",
    "heads-up": "📢 Heads-up",
    "question": "❓ Question",
    "answer": "💡 Answer",
    "escalation": "🚨 Escalation",
}

_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "matrix_create_thread",
        "description": (
            "Create a new Matrix thread and return its root event ID. "
            "Orchestrator calls this at the start of each pipeline phase. "
            "The returned event_id becomes {thread_root} for all agents in that phase."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": (
                        "Agent role posting (architect, po, pm, coder, reviewer, qa, security)"
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Thread topic (e.g. 'issue-42-implementation', 'planning-council')"
                    ),
                },
            },
            "required": ["role", "topic"],
        },
    },
    {
        "name": "matrix_read",
        "description": (
            "Read recent messages from the HomePilot Agents Matrix room. "
            "Returns parsed messages with role, type, content, and event ID. "
            "Pass thread_root to read only messages within a specific thread."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": (
                        "Agent role to read as (architect, po, pm, coder, reviewer, qa, security)"
                    ),
                },
                "limit": {
                    "type": ["integer", "null"],
                    "description": "Max messages to return (default 50)",
                },
                "since": {
                    "type": ["string", "null"],
                    "description": "Event ID — only return messages after this event",
                },
                "filter_type": {
                    "type": ["string", "null"],
                    "description": (
                        "Filter by message type: position, challenge, agreement, "
                        "converged, decision, heads-up, question, answer, escalation"
                    ),
                },
                "thread_root": {
                    "type": ["string", "null"],
                    "description": "Event ID of thread root — only return messages in this thread",
                },
            },
            "required": ["role"],
        },
    },
    {
        "name": "matrix_post",
        "description": (
            "Post a message to the HomePilot Agents Matrix room as a specific agent role. "
            "Messages are tagged with role and type. "
            "Pass thread_root to post within a specific thread (always do this when one exists). "
            "Keep content to 1 line; link to handoff files for full details."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": (
                        "Agent role posting (architect, po, pm, coder, reviewer, qa, security)"
                    ),
                },
                "msg_type": {
                    "type": "string",
                    "description": (
                        "Message type: position, challenge, agreement, converged, "
                        "decision, heads-up, question, answer, escalation"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Message content (1 line; link to handoff file for full details)"
                    ),
                },
                "thread_root": {
                    "type": ["string", "null"],
                    "description": "Event ID of thread root — post within this thread",
                },
            },
            "required": ["role", "msg_type", "content"],
        },
    },
    {
        "name": "matrix_read_positions",
        "description": (
            "Read only position messages from the Matrix room. "
            "Convenience tool — equivalent to matrix_read with filter_type='position'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Agent role to read as",
                },
                "since": {
                    "type": ["string", "null"],
                    "description": "Event ID — only return positions after this event",
                },
                "thread_root": {
                    "type": ["string", "null"],
                    "description": "Event ID of thread root — filter to this thread",
                },
            },
            "required": ["role"],
        },
    },
    {
        "name": "matrix_read_converged",
        "description": (
            "Read converged decisions from the Matrix room. "
            "Returns the text of all CONVERGED messages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Agent role to read as",
                },
                "thread_root": {
                    "type": ["string", "null"],
                    "description": "Event ID of thread root — filter to this thread",
                },
            },
            "required": ["role"],
        },
    },
    {
        "name": "matrix_who_is_here",
        "description": (
            "List agent members in the HomePilot Agents Matrix room. "
            "Returns which bot accounts are joined."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Agent role to query as",
                },
            },
            "required": ["role"],
        },
    },
    {
        "name": "matrix_post_reaction",
        "description": (
            "Post a reaction (emoji) to a specific message in the Matrix room. "
            "Use for quick acknowledgement (e.g. +1 for agreement) instead of "
            "posting a separate 'agreement' message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Agent role reacting",
                },
                "event_id": {
                    "type": "string",
                    "description": "Event ID to react to",
                },
                "emoji": {
                    "type": "string",
                    "description": "Emoji to react with (e.g. +1, -1, eyes, rocket)",
                },
            },
            "required": ["role", "event_id", "emoji"],
        },
    },
]


def _load_tokens() -> dict[str, str]:
    secrets_dir = os.environ.get("HP_SECRETS_DIR", "/tmp/opencode/secrets")
    token_file = os.path.join(secrets_dir, "matrix_tokens.json")
    try:
        with open(token_file) as f:
            tokens: dict[str, str] = json.load(f)
            return tokens
    except FileNotFoundError:
        raise RuntimeError(f"No Matrix tokens at {token_file}") from None


def _load_room_id() -> str:
    room_id = os.environ.get("MATRIX_ROOM_ID", "")
    if room_id:
        return room_id
    secrets_dir = os.environ.get("HP_SECRETS_DIR", "/tmp/opencode/secrets")
    room_file = os.path.join(secrets_dir, "matrix_room.json")
    try:
        with open(room_file) as f:
            data: dict[str, str] = json.load(f)
        room_id = data.get("room_id", "")
        if not room_id:
            raise RuntimeError("room_id missing from room info file")
        return room_id
    except FileNotFoundError:
        raise RuntimeError(f"No Matrix room info at {room_file}") from None


def _parse_sender_role(sender: str) -> str:
    """Extract role from sender like @hp-architect:example.com."""
    m = re.match(r"@hp-([a-z]+):example\.com", sender)
    if m:
        return m.group(1)
    return "unknown"


def _parse_msg_type(body: str) -> str:
    for msg_type, emoji_prefix in _MSG_TYPE_EMOJI.items():
        if body.startswith(emoji_prefix):
            return msg_type
    return "unknown"


def _thread_root_of(event: dict[str, Any]) -> str | None:
    """Return the thread root event ID if this event is in a thread."""
    relates = event.get("content", {}).get("m.relates_to", {})
    if relates.get("rel_type") == "m.thread":
        return str(relates.get("event_id", ""))
    return None


class MatrixClient:
    def __init__(self, role: str) -> None:
        if role not in _ROLES:
            raise ValueError(f"Unknown role: {role}. Must be one of: {', '.join(_ROLES)}")
        self._role = role
        self._tokens = _load_tokens()
        user_id = f"@hp-{role}:example.com"
        token = (
            self._tokens.get(user_id) or self._tokens.get(role) or self._tokens.get(f"hp-{role}")
        )
        if not token:
            raise RuntimeError(f"No token for {user_id}")
        self._token = token
        self._room_id = _load_room_id()
        self._base = os.environ.get("MATRIX_SERVER", "https://matrix.example.com")
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def read_messages(
        self,
        limit: int = 50,
        since: str | None = None,
        filter_type: str | None = None,
        thread_root: str | None = None,
    ) -> list[dict[str, Any]]:
        # `since` is an event_id, not a Matrix /messages?from= pagination token.
        # Strategy: fetch a generous backlog, then post-filter to "messages
        # strictly after the event whose id == since". This works without
        # depending on /sync semantics.
        session = await self._get_session()
        url = f"{self._base}/_matrix/client/v3/rooms/{self._room_id}/messages"
        backlog = max(limit * 4, 200) if since else limit * 3
        params: dict[str, Any] = {"limit": backlog, "dir": "b"}

        async with session.get(url, headers=self._headers(), params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Matrix read failed ({resp.status}): {text}")
            data = await resp.json()

        # /messages?dir=b returns newest-first. Reverse for chronological order.
        chunk_chronological = list(reversed(data.get("chunk", [])))

        # Post-filter: drop everything up to and including the event matching `since`.
        if since:
            for i, event in enumerate(chunk_chronological):
                if event.get("event_id") == since:
                    chunk_chronological = chunk_chronological[i + 1 :]
                    break
            # If `since` not found in backlog, return nothing rather than
            # incorrectly returning all messages — caller should widen backlog.
            else:
                chunk_chronological = []

        messages = []
        for event in chunk_chronological:
            if event.get("type") != "m.room.message":
                continue
            content = event.get("content", {})
            body = content.get("body", "")
            sender = event.get("sender", "unknown")
            event_id = event.get("event_id", "")
            ts = event.get("origin_server_ts", 0)

            msg_type = _parse_msg_type(body)
            role = _parse_sender_role(sender)
            evt_thread_root = _thread_root_of(event)

            # Thread filter
            if thread_root is not None and evt_thread_root != thread_root:
                continue

            # Hide thread-root markers from regular reads (they're orchestrator-only).
            if body.startswith(_THREAD_ROOT_PREFIX):
                continue

            # Type filter
            if filter_type and msg_type != filter_type:
                continue

            messages.append(
                {
                    "event_id": event_id,
                    "role": role,
                    "msg_type": msg_type,
                    "content": body,
                    "sender": sender,
                    "ts": ts,
                    "thread_root": evt_thread_root,
                }
            )

            if len(messages) >= limit:
                break

        return messages

    async def post_message(
        self,
        msg_type: str,
        content: str,
        thread_root: str | None = None,
        raw_body: str | None = None,
    ) -> str:
        # raw_body bypasses the emoji prefix (used for THREAD-ROOT markers).
        if raw_body is not None:
            body = raw_body
        else:
            emoji_prefix = _MSG_TYPE_EMOJI.get(msg_type, msg_type)
            body = f"{emoji_prefix}: {content}"

        session = await self._get_session()
        # Matrix C-S API: PUT /rooms/{room}/send/{eventType}/{txnId} (idempotent).
        txn_id = f"_post_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        url = f"{self._base}/_matrix/client/v3/rooms/{self._room_id}/send/m.room.message/{txn_id}"

        msg_content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": body,
        }
        if thread_root:
            msg_content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_root,
            }

        async with session.put(url, headers=self._headers(), json=msg_content) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise RuntimeError(f"Matrix post failed ({resp.status}): {text}")
            data = await resp.json()

        return str(data.get("event_id", ""))

    async def get_members(self) -> list[dict[str, Any]]:
        session = await self._get_session()
        url = f"{self._base}/_matrix/client/v3/rooms/{self._room_id}/members"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Matrix members failed ({resp.status}): {text}")
            data = await resp.json()

        members = []
        for event in data.get("chunk", []):
            if event.get("type") != "m.room.member":
                continue
            state_key = event.get("state_key", "")
            if ":example.com" not in state_key:
                continue
            display = event.get("content", {}).get("displayname", state_key)
            members.append({"user": state_key, "displayname": display})
        return members

    async def post_reaction(self, event_id: str, emoji: str) -> str:
        session = await self._get_session()
        url = f"{self._base}/_matrix/client/v3/rooms/{self._room_id}/send/m.reaction"
        txn_id = f"_react_{event_id}_{emoji}"
        body = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": emoji,
            }
        }
        async with session.put(f"{url}/{txn_id}", headers=self._headers(), json=body) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise RuntimeError(f"Matrix reaction failed ({resp.status}): {text}")
            data = await resp.json()
        return str(data.get("event_id", ""))


async def _handle_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    role = arguments.get("role", "")
    if role not in _ROLES:
        raise ValueError(f"Unknown role: {role}. Must be one of: {', '.join(_ROLES)}")

    client = MatrixClient(role)
    try:
        if name == "matrix_create_thread":
            topic = arguments["topic"]
            # Raw body so _parse_msg_type returns "unknown" and the marker is
            # also filtered out of read_messages (THREAD-ROOT prefix check).
            event_id = await client.post_message(
                msg_type="",
                content="",
                thread_root=None,
                raw_body=f"{_THREAD_ROOT_PREFIX} {topic}",
            )
            return [TextContent(type="text", text=json.dumps({"event_id": event_id}))]

        elif name == "matrix_read":
            limit = int(arguments.get("limit") or 50)
            since = arguments.get("since") or None
            filter_type = arguments.get("filter_type") or None
            thread_root = arguments.get("thread_root") or None
            messages = await client.read_messages(
                limit=limit,
                since=since,
                filter_type=filter_type,
                thread_root=thread_root,
            )
            return [TextContent(type="text", text=json.dumps(messages, indent=2))]

        elif name == "matrix_post":
            msg_type = arguments["msg_type"]
            content = arguments["content"]
            thread_root = arguments.get("thread_root") or None
            event_id = await client.post_message(
                msg_type=msg_type,
                content=content,
                thread_root=thread_root,
            )
            return [
                TextContent(
                    type="text", text=json.dumps({"event_id": event_id, "status": "posted"})
                )
            ]

        elif name == "matrix_read_positions":
            since = arguments.get("since") or None
            thread_root = arguments.get("thread_root") or None
            messages = await client.read_messages(
                filter_type="position",
                since=since,
                thread_root=thread_root,
            )
            return [TextContent(type="text", text=json.dumps(messages, indent=2))]

        elif name == "matrix_read_converged":
            thread_root = arguments.get("thread_root") or None
            messages = await client.read_messages(
                filter_type="converged",
                thread_root=thread_root,
            )
            # Extract converged decision text (strip emoji prefix)
            decisions = []
            for msg in messages:
                body = msg.get("content", "")
                m = re.search(r"CONVERGED:\s*(.+)", body, re.IGNORECASE)
                if m:
                    decisions.append({"sender": msg.get("role"), "body": m.group(1).strip()})
                else:
                    decisions.append({"sender": msg.get("role"), "body": body})
            return [TextContent(type="text", text=json.dumps(decisions, indent=2))]

        elif name == "matrix_who_is_here":
            members = await client.get_members()
            return [TextContent(type="text", text=json.dumps({"members": members}))]

        elif name == "matrix_post_reaction":
            event_id = arguments["event_id"]
            emoji = arguments["emoji"]
            result_event_id = await client.post_reaction(event_id=event_id, emoji=emoji)
            return [
                TextContent(
                    type="text", text=json.dumps({"event_id": result_event_id, "status": "reacted"})
                )
            ]

        else:
            raise ValueError(f"Unknown tool: {name}")

    finally:
        await client.close()


def create_server() -> Server:
    server = Server("homepilot-matrix-mcp")

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in _TOOL_DEFINITIONS
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        return await _handle_tool(name, arguments)

    return server


async def run_server() -> None:
    srv = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await srv.run(read_stream, write_stream, srv.create_initialization_options())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
