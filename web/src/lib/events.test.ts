import { describe, it, expect } from 'vitest';
import {
	nextBackoffMs,
	isArtifactEventType,
	streamUrl,
	STREAM_PATH,
	ARTIFACT_EVENT_TYPES,
} from './events';

describe('nextBackoffMs', () => {
	it('grows exponentially from 1s', () => {
		expect(nextBackoffMs(0)).toBe(1000);
		expect(nextBackoffMs(1)).toBe(2000);
		expect(nextBackoffMs(2)).toBe(4000);
		expect(nextBackoffMs(3)).toBe(8000);
	});

	it('saturates at 30s', () => {
		expect(nextBackoffMs(5)).toBe(30000);
		expect(nextBackoffMs(20)).toBe(30000);
	});

	it('treats negative or fractional attempts safely', () => {
		expect(nextBackoffMs(-3)).toBe(1000);
		expect(nextBackoffMs(1.9)).toBe(2000);
	});
});

describe('isArtifactEventType', () => {
	it('recognises every known lifecycle/drift event', () => {
		for (const t of ARTIFACT_EVENT_TYPES) {
			expect(isArtifactEventType(t)).toBe(true);
		}
	});

	it('rejects keepalive and unknown frames', () => {
		expect(isArtifactEventType('ping')).toBe(false);
		expect(isArtifactEventType('message')).toBe(false);
		expect(isArtifactEventType('')).toBe(false);
	});
});

describe('streamUrl', () => {
	it('defaults to the root-mounted artifacts stream path', () => {
		expect(streamUrl('')).toBe(STREAM_PATH);
		expect(streamUrl('')).toBe('/artifacts/events/stream');
	});

	it('prefixes an explicit API base', () => {
		expect(streamUrl('https://api.example')).toBe('https://api.example/artifacts/events/stream');
	});
});

/**
 * A reconnect resyncs (#701).
 *
 * `onopen` only set the "connected" badge. After a gap of up to 30 s the badge
 * cleared while every page had missed whatever happened in the gap - the stream
 * does not replay, and the server drops events for a subscriber whose queue is
 * full. On every REconnect the bus now tells every subscriber to re-read
 * (`resync`), so a page that missed events fetches the truth instead of showing
 * a stale state under a green badge.
 *
 * Teeth: drop the resync dispatch from `onopen` and the reconnect test fails;
 * dispatch it on the FIRST open too and the first-open test fails.
 */
describe('reconnect resync', () => {
	class FakeES {
		static last: FakeES | null = null;
		onopen: (() => void) | null = null;
		onerror: (() => void) | null = null;
		closed = false;
		constructor() {
			FakeES.last = this;
		}
		addEventListener() {}
		close() {
			this.closed = true;
		}
	}

	it('tells every subscriber to re-read after a reconnect, not after the first open', async () => {
		const { vi } = await import('vitest');
		vi.useFakeTimers();
		vi.stubGlobal('EventSource', FakeES);
		vi.resetModules();
		const events = await import('./events');
		const seen: string[] = [];
		const unsub = events.onArtifactEvent((e) => seen.push(e.type));
		try {
			events.startEventStream();
			FakeES.last!.onopen!();
			expect(seen).toEqual([]);

			FakeES.last!.onerror!();
			await vi.advanceTimersByTimeAsync(1000);
			FakeES.last!.onopen!();
			expect(seen).toEqual(['resync']);
		} finally {
			unsub();
			events.stopEventStream();
			vi.unstubAllGlobals();
			vi.useRealTimers();
		}
	});
});
