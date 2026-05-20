/**
 * Token storage security: verifies token is kept in memory, not localStorage.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { setToken, getToken } from './api';

beforeEach(() => {
	setToken('');
});

describe('token storage', () => {
	it('getToken returns empty string before any setToken call', () => {
		expect(getToken()).toBe('');
	});

	it('setToken/getToken round-trip works in memory', () => {
		setToken('my-secret-token');
		expect(getToken()).toBe('my-secret-token');
	});

	it('setToken does NOT write to localStorage', () => {
		const spy = { setItem: (..._args: unknown[]) => {} };
		const original = globalThis.localStorage;
		Object.defineProperty(globalThis, 'localStorage', { value: spy, writable: true });

		let called = false;
		spy.setItem = () => { called = true; };
		setToken('should-not-hit-storage');
		expect(called).toBe(false);

		Object.defineProperty(globalThis, 'localStorage', { value: original, writable: true });
	});

	it('getToken does NOT read from localStorage', () => {
		const spy = { getItem: (_k: string) => 'from-storage' };
		const original = globalThis.localStorage;
		Object.defineProperty(globalThis, 'localStorage', { value: spy, writable: true });

		setToken('in-memory-value');
		expect(getToken()).toBe('in-memory-value');

		Object.defineProperty(globalThis, 'localStorage', { value: original, writable: true });
	});

	it('clearing token via setToken empty string works', () => {
		setToken('abc');
		setToken('');
		expect(getToken()).toBe('');
	});
});
