/**
 * ApiError message/detail normalization: a structured server `detail` (object)
 * must never reach a toast as raw JSON, and 409 conflicts must read cleanly.
 */
import { describe, it, expect } from 'vitest';
import { ApiError } from './api';

describe('ApiError.humanize', () => {
	it('maps known statuses to friendly copy', () => {
		expect(ApiError.humanize(401, 'x')).toMatch(/token/i);
		expect(ApiError.humanize(403, 'x')).toMatch(/permission/i);
		expect(ApiError.humanize(404, 'x')).toMatch(/not found/i);
		expect(ApiError.humanize(429, 'x')).toMatch(/too many/i);
		expect(ApiError.humanize(500, 'x')).toMatch(/server/i);
	});

	it('adds a 409 case that surfaces the detail when present', () => {
		expect(ApiError.humanize(409, 'artifact has an active task')).toBe(
			'artifact has an active task',
		);
	});

	it('adds a 409 case with a generic fallback when detail is empty', () => {
		expect(ApiError.humanize(409, '')).toMatch(/conflict/i);
	});
});

describe('ApiError.detailToString', () => {
	it('passes strings through unchanged', () => {
		expect(ApiError.detailToString('boom')).toBe('boom');
	});

	it('extracts a reason/message/error field from an object', () => {
		expect(ApiError.detailToString({ reason: 'active task' })).toBe('active task');
		expect(ApiError.detailToString({ message: 'nope' })).toBe('nope');
		expect(ApiError.detailToString({ error: 'bad' })).toBe('bad');
	});

	it('stringifies an object with no known field', () => {
		expect(ApiError.detailToString({ code: 7 })).toBe('{"code":7}');
	});
});

describe('ApiError construction', () => {
	it('collapses a structured detail object so raw JSON never reaches the toast', () => {
		const body = JSON.stringify({ detail: { reason: 'artifact has an active task' } });
		const err = new ApiError(409, body);
		expect(err.status).toBe(409);
		expect(err.detail).toBe('artifact has an active task');
		expect(err.message).toBe('artifact has an active task');
		// The raw JSON braces must not survive into the user-facing message.
		expect(err.message).not.toContain('{');
	});

	it('keeps a plain string detail', () => {
		const err = new ApiError(404, JSON.stringify({ detail: 'gone' }));
		expect(err.detail).toBe('gone');
	});

	it('keeps a non-JSON body as-is', () => {
		const err = new ApiError(500, 'upstream exploded');
		expect(err.detail).toBe('upstream exploded');
		expect(err.message).toMatch(/server/i);
	});
});
