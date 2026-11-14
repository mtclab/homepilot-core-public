import { describe, it, expect } from 'vitest';
import { forgetAll, isRunning, leftOutSentence, putOffSentence, snoozeHolds, upgradeInTurn } from './attention';
import type { AgentUpgrade, AttentionSnooze, Host, Task } from './api';

/**
 * The browser's half of the quiet Overview's rules (3.12). `snoozeHolds` and
 * `isRunning` MUST equal their server twins (dashboard/attention.py
 * `snooze_holds`, dashboard/service.py `is_running`); the cases here are the
 * ones tests/test_quiet_overview.py drives on the server.
 */
const NOW = Date.parse('2026-11-13T12:00:00Z');
const snooze = (severity: AttentionSnooze['severity'], until = '2026-11-20T12:00:00Z'): AttentionSnooze => ({
	key: 'k',
	severity,
	until,
	by: 'owner (hp_owner000001)',
	at: '2026-11-13T12:00:00Z',
});

describe('snoozeHolds', () => {
	it('holds a line until its week is out', () => {
		expect(snoozeHolds(snooze('warning'), 'warning', NOW)).toBe(true);
		expect(snoozeHolds(snooze('warning', '2026-11-13T11:59:59Z'), 'warning', NOW)).toBe(false);
		expect(snoozeHolds(undefined, 'notice', NOW)).toBe(false);
	});

	it('ends early when the line becomes more serious, not when it calms down', () => {
		expect(snoozeHolds(snooze('warning'), 'critical', NOW)).toBe(false);
		expect(snoozeHolds(snooze('notice'), 'warning', NOW)).toBe(false);
		expect(snoozeHolds(snooze('critical'), 'warning', NOW)).toBe(true);
	});
});

describe('isRunning', () => {
	const h = (over: Partial<Host>): Host => ({ id: 'x', hostname: 'x', ...over }) as Host;
	it('is Proxmox running, or an undescribed machine that says online - never gone', () => {
		expect(isRunning(h({ pve_status: 'running' }))).toBe(true);
		expect(isRunning(h({ pve_status: 'stopped', status: 'online' }))).toBe(false);
		expect(isRunning(h({ pve_status: null, status: 'online' } as unknown as Partial<Host>))).toBe(true);
		expect(isRunning(h({ pve_status: 'running', absent_since: '2026-11-01T00:00:00Z' }))).toBe(false);
		expect(isRunning(h({ pve_status: 'running', is_template: 1 }))).toBe(false);
	});
});

describe('footer sentences', () => {
	it('says what was left out and why, in one sentence', () => {
		expect(leftOutSentence({ superseded: 8, older: 0 })).toBe(
			'8 failed runs were superseded by later successes and are in Records.',
		);
		expect(leftOutSentence({ superseded: 0, older: 1 })).toBe('1 failed run more than a day old is in Records.');
		expect(leftOutSentence({ superseded: 0, older: 0 })).toBe('');
		expect(putOffSentence([snooze('warning'), snooze('notice')])).toBe('2 put off until 20 Nov');
	});
});

describe('forgetAll', () => {
	it('tries every machine and names the ones that could not be forgotten', async () => {
		const tried: string[] = [];
		const out = await forgetAll(
			[
				{ id: 'a', hostname: 'old-a' },
				{ id: 'b', hostname: 'old-b' },
				{ id: 'c', hostname: 'old-c' },
			],
			async (id) => {
				tried.push(id);
				if (id === 'b') throw new Error('409 still reported');
			},
		);
		expect(tried).toEqual(['a', 'b', 'c']);
		expect(out).toEqual({ forgotten: ['old-a', 'old-c'], failed: [{ hostname: 'old-b', error: '409 still reported' }] });
	});
});

describe('upgradeInTurn', () => {
	const row = (name: string, hostId: string | null): AgentUpgrade => ({
		agent_id: `ag-${name}`,
		hostname: name,
		agent_version: 'v3.10.0',
		control_version: '3.11.0',
		behind: true,
		host_id: hostId,
	});

	it('follows each upgrade to its end before the next, and stops at the first failure', async () => {
		const events: string[] = [];
		const polls: Record<string, number> = {};
		const out = await upgradeInTurn(
			[row('a', 'h-a'), row('skip', null), row('b', 'h-b'), row('c', 'h-c')],
			{
				install: async (hostId) => {
					events.push(`install ${hostId}`);
					return { task_id: hostId };
				},
				getTask: async (id) => {
					polls[id] = (polls[id] ?? 0) + 1;
					events.push(`poll ${id}`);
					const status = polls[id] < 2 ? 'running' : id === 'h-b' ? 'failed' : 'succeeded';
					return { id, status, error: status === 'failed' ? 'boom' : null } as Task;
				},
				wait: async () => undefined,
			},
			(id, state) => events.push(`${id} ${state}`),
		);
		expect(events).toEqual([
			'ag-a upgrading',
			'install h-a',
			'poll h-a',
			'poll h-a',
			'ag-a done',
			'ag-skip skipped',
			'ag-b upgrading',
			'install h-b',
			'poll h-b',
			'poll h-b',
			'ag-b failed',
		]);
		expect(out).toEqual({ done: ['a'], stopped: { hostname: 'b', error: 'boom' } });
	});

	it('stops when the install itself is refused, and says why', async () => {
		const out = await upgradeInTurn(
			[row('a', 'h-a'), row('b', 'h-b')],
			{
				install: async () => {
					throw new Error('409 not eligible');
				},
				getTask: async () => ({}) as Task,
			},
			() => undefined,
		);
		expect(out).toEqual({ done: [], stopped: { hostname: 'a', error: '409 not eligible' } });
	});
});
