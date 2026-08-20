/**
 * The operator-facing "API Base URL" setting must actually move the traffic.
 *
 * Regression gate for the 2026-08-16 review finding: the value was written to
 * localStorage['hp_api_base'] and never read back, so Settings reported
 * "Settings saved" while every request kept going to the build-time origin.
 * These tests forbid that whole class — they assert the RESOLUTION RULE and,
 * more importantly, that the real request path (api.ts) and the real SSE path
 * (events.ts) both go through it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
	API_BASE_STORAGE_KEY,
	getApiBase,
	normalizeApiBase,
	readStoredApiBase,
	resolveApiBase,
	writeStoredApiBase,
} from './apiBase';
import { api } from './api';
import { streamUrl } from './events';

beforeEach(() => {
	// Restore any globals a previous test replaced (localStorage/fetch) BEFORE
	// touching them.
	vi.unstubAllGlobals();
	vi.unstubAllEnvs();
	localStorage.clear();
});

afterEach(() => {
	vi.unstubAllGlobals();
	vi.unstubAllEnvs();
	vi.restoreAllMocks();
	localStorage.clear();
});

describe('resolveApiBase', () => {
	it('prefers the stored operator override over the env default', () => {
		expect(resolveApiBase('https://override.example', 'https://env.example')).toBe(
			'https://override.example',
		);
	});

	it('falls back to the env default when there is no override', () => {
		expect(resolveApiBase(null, 'https://env.example')).toBe('https://env.example');
		expect(resolveApiBase('', 'https://env.example')).toBe('https://env.example');
		expect(resolveApiBase('   ', 'https://env.example')).toBe('https://env.example');
	});

	it('resolves to same-origin when neither is set', () => {
		expect(resolveApiBase(null, '')).toBe('');
		expect(resolveApiBase(undefined, undefined)).toBe('');
	});

	it('strips trailing slashes so `${base}${path}` never doubles up', () => {
		expect(resolveApiBase('http://localhost:8000/', '')).toBe('http://localhost:8000');
		expect(normalizeApiBase('http://localhost:8000///')).toBe('http://localhost:8000');
	});
});

describe('stored override', () => {
	it('round-trips through localStorage under the documented key', () => {
		expect(writeStoredApiBase('https://api.example')).toBe(true);
		expect(localStorage.getItem(API_BASE_STORAGE_KEY)).toBe('https://api.example');
		expect(readStoredApiBase()).toBe('https://api.example');
		expect(getApiBase()).toBe('https://api.example');
	});

	it('a blank value clears the override', () => {
		writeStoredApiBase('https://api.example');
		writeStoredApiBase('   ');
		expect(readStoredApiBase()).toBeNull();
		expect(getApiBase()).toBe('');
	});

	it('uses the env default when no override is stored', () => {
		vi.stubEnv('VITE_API_BASE', 'https://env.example');
		expect(getApiBase()).toBe('https://env.example');
	});

	it('the override still wins over the env default', () => {
		vi.stubEnv('VITE_API_BASE', 'https://env.example');
		writeStoredApiBase('https://override.example');
		expect(getApiBase()).toBe('https://override.example');
	});
});

describe('SSR safety', () => {
	it('resolves without touching localStorage when there is none (server render)', () => {
		vi.stubGlobal('localStorage', undefined);
		vi.stubEnv('VITE_API_BASE', 'https://env.example');
		expect(() => getApiBase()).not.toThrow();
		expect(readStoredApiBase()).toBeNull();
		expect(getApiBase()).toBe('https://env.example');
		expect(writeStoredApiBase('https://nope.example')).toBe(false);
	});

	it('survives a browser that throws on storage access', () => {
		vi.stubGlobal('localStorage', {
			getItem() {
				throw new Error('storage disabled');
			},
			setItem() {
				throw new Error('storage disabled');
			},
			removeItem() {
				throw new Error('storage disabled');
			},
		});
		expect(getApiBase()).toBe('');
		expect(writeStoredApiBase('https://api.example')).toBe(false);
	});
});

describe('the setting reaches the wire', () => {
	function mockFetch() {
		const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
			void url;
			return {
				ok: true,
				status: 200,
				json: async () => ({}),
				text: async () => '',
			};
		});
		vi.stubGlobal('fetch', fetchMock);
		return fetchMock;
	}

	it('api requests go to the stored override, not the env default', async () => {
		vi.stubEnv('VITE_API_BASE', '');
		writeStoredApiBase('https://api.example');
		const fetchMock = mockFetch();

		await api.me();

		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(fetchMock.mock.calls[0][0]).toBe('https://api.example/auth/me');
	});

	it('a base change takes effect without a reload', async () => {
		const fetchMock = mockFetch();
		writeStoredApiBase('https://first.example');
		await api.me();
		writeStoredApiBase('https://second.example');
		await api.me();

		expect(fetchMock.mock.calls[0][0]).toBe('https://first.example/auth/me');
		expect(fetchMock.mock.calls[1][0]).toBe('https://second.example/auth/me');
	});

	it('same-origin when the operator clears the override', async () => {
		const fetchMock = mockFetch();
		await api.me();
		expect(fetchMock.mock.calls[0][0]).toBe('/auth/me');
	});

	it('the SSE stream follows the same base as the requests', () => {
		writeStoredApiBase('https://api.example');
		expect(streamUrl()).toBe('https://api.example/artifacts/events/stream');
		writeStoredApiBase('');
		expect(streamUrl()).toBe('/artifacts/events/stream');
	});
});
