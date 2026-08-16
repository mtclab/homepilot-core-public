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
