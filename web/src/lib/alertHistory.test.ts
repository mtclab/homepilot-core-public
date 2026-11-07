import { describe, it, expect } from 'vitest';
import { deliveryText, destination, humanDuration, resolvedText } from './alertHistory';
import type { AlertEvent } from './api';

const base: AlertEvent = {
	id: 1,
	rule_id: 'r',
	rule_name: 'Load too high',
	rule_kind: 'threshold',
	metric: 'load.1m',
	hostname: 'h',
	event: 'resolved',
	value: 1,
	since: null,
	duration_seconds: 720,
	reason: null,
	delivered_to: null,
	at: '2026-11-07T00:00:00Z',
};

describe('alert history sentences', () => {
	it('durations read like the backend writes them', () => {
		expect(humanDuration(42)).toBe('42 s');
		expect(humanDuration(420)).toBe('7 min');
		expect(humanDuration(7200)).toBe('2 h');
		expect(humanDuration(3 * 86400)).toBe('3 d');
		expect(humanDuration(null)).toBe('');
	});

	it('a threshold episode names its rule; a dark one says dark; a stopped one says why', () => {
		expect(resolvedText(base)).toBe('Load too high for 12 min');
		expect(resolvedText({ ...base, rule_kind: 'nodata', duration_seconds: 420 })).toBe('went dark for 7 min');
		expect(
			resolvedText({ ...base, rule_kind: 'nodata', reason: 'the hypervisor reports the guest stopped' }),
		).toBe('counted dark for 12 min, then the hypervisor reports the guest stopped');
	});

	it('delivery: unknown is not "nobody"', () => {
		expect(deliveryText(base)).toBeNull();
		expect(deliveryText({ ...base, delivered_to: [] })).toBe('nobody was notified');
		expect(deliveryText({ ...base, delivered_to: ['phone'] })).toBe('sent to phone');
	});

	it('a disabled-only setup is "no channel", and untested is not ok', () => {
		const ch = { id: 1, kind: 'ntfy' as const, name: 'p', enabled: false, state: 'disabled' as const, last_ok_at: null, last_error_at: null };
		expect(destination([ch]).tone).toBe('warn');
		expect(destination([{ ...ch, enabled: true, state: 'untested' }]).tone).toBe('warn');
		expect(destination([{ ...ch, enabled: true, state: 'ok' }]).tone).toBe('ok');
		expect(destination([{ ...ch, enabled: true, state: 'failing' }]).tone).toBe('danger');
	});
});
