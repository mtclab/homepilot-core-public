import { writable, type Writable } from 'svelte/store';

import { getApiBase } from './apiBase';

// The API lives at the origin root (see api.ts) — NOT under the UI `base`
// (`/ui`). The base is empty in every default build, so the stream is
// same-origin and the browser EventSource sends the session cookie
// automatically. It resolves through the SAME accessor api.ts uses, so an
// operator's Settings override moves the stream with the requests.

// Artifact-lifecycle + drift event names emitted by the backend SSE bus
// (src/homepilot/artifacts/lifecycle.py + reconciler/drift.py). `ping` frames
// are keepalive only and are intentionally not listed here.
export const ARTIFACT_EVENT_TYPES = [
	'artifact_proposed',
	'artifact_approved',
	'artifact_rejected',
	'artifact_applied',
	'artifact_failed',
	'artifact_superseded',
	'artifact_revoked',
	'artifact_drifted',
] as const;

export type ArtifactEventType = (typeof ARTIFACT_EVENT_TYPES)[number];

export interface ArtifactEvent {
	type: ArtifactEventType;
	data: Record<string, unknown>;
	at: number;
}

// Exact SSE endpoint: `GET /artifacts/events/stream` (artifacts router mounted
// at prefix `/artifacts`).
export const STREAM_PATH = '/artifacts/events/stream';

export function streamUrl(apiBase: string = getApiBase()): string {
	return `${apiBase}${STREAM_PATH}`;
}

const BACKOFF_BASE_MS = 1000;
const BACKOFF_MAX_MS = 30000;

// Exponential backoff (capped) for the reconnect loop. `attempt` is 0-based:
// attempt 0 → 1s, 1 → 2s, … saturating at 30s. Pure so it can be unit-tested.
export function nextBackoffMs(attempt: number): number {
	const n = Math.max(0, Math.floor(attempt));
	return Math.min(BACKOFF_BASE_MS * 2 ** n, BACKOFF_MAX_MS);
}

export function isArtifactEventType(t: string): t is ArtifactEventType {
	return (ARTIFACT_EVENT_TYPES as readonly string[]).includes(t);
}

type Handler = (e: ArtifactEvent) => void;
const handlers = new Set<Handler>();

// Subscribe to FUTURE artifact events only (never replays the last one).
// Returns an unsubscribe fn. Ideal for a page that already loads on mount and
// just wants to re-fetch when something changes.
export function onArtifactEvent(handler: Handler): () => void {
	handlers.add(handler);
	return () => {
		handlers.delete(handler);
	};
}

/**
 * Whether the live-update stream is currently connected (#435).
 *
 * The stream reconnected silently with backoff, so when it was down the UI just
 * quietly stopped updating - with no badge, and no way for an operator to tell
 * "nothing is happening" from "I am not being told what is happening". On an
 * ops console those are opposite conclusions.
 */
export const streamConnected: Writable<boolean> = writable(false);

let es: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let attempt = 0;
let started = false;

function dispatch(type: ArtifactEventType, raw: string): void {
	let data: Record<string, unknown> = {};
	try {
		data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
	} catch {
		data = {};
	}
	const evt: ArtifactEvent = { type, data, at: Date.now() };
	// Copy the set so a handler that unsubscribes mid-dispatch can't disturb the
	// iteration, and isolate handler errors so one bad page can't wedge the bus.
	for (const h of [...handlers]) {
		try {
			h(evt);
		} catch {
			/* a subscriber error must never break the stream */
		}
	}
}

function clearReconnect(): void {
	if (reconnectTimer) {
		clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}
}

function scheduleReconnect(): void {
	if (!started || reconnectTimer) return;
	const delay = nextBackoffMs(attempt);
	attempt += 1;
	reconnectTimer = setTimeout(() => {
		reconnectTimer = null;
		open();
	}, delay);
}

function open(): void {
	// SSR / non-browser: no EventSource. Never throw — the pages fall back to
	// their own load()/Refresh path when the stream is unavailable.
	if (typeof EventSource === 'undefined') return;
	clearReconnect();
	try {
		es = new EventSource(streamUrl(), { withCredentials: true });
	} catch {
		scheduleReconnect();
		return;
	}
	es.onopen = () => {
		attempt = 0;
		streamConnected.set(true);
	};
	for (const t of ARTIFACT_EVENT_TYPES) {
		es.addEventListener(t, (ev) => dispatch(t, (ev as MessageEvent).data));
	}
	es.onerror = () => {
		streamConnected.set(false);
		// A native EventSource retries on its own at a fixed cadence; we take over
		// so a flapping backend backs off (and we respect the server's per-IP
		// connection cap) instead of hammering it.
		if (es) {
			es.close();
			es = null;
		}
		scheduleReconnect();
	};
}

// Idempotent: opens exactly one stream no matter how often it's called. Safe to
// invoke on every layout session-sync.
export function startEventStream(): void {
	if (started) return;
	started = true;
	attempt = 0;
	open();
}

// Closes the stream and cancels any pending reconnect. Call on logout.
export function stopEventStream(): void {
	started = false;
	streamConnected.set(false);
	clearReconnect();
	if (es) {
		es.close();
		es = null;
	}
	attempt = 0;
}
