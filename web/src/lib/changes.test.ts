import { describe, it, expect } from 'vitest';
import { inboxState, outcomeSentence, recentlyFinished, runningLine, hhmm } from './changes';
import type { Artifact } from './api';

/**
 * The words of the Changes inbox (owner feedback 2026-11-08). Each state says
 * what happens next; nothing is a bare status.
 */
const base: Artifact = {
	id: 'a',
	status: 'proposed',
	kind: 'proxmox-api-sequence',
	intent: 'Back up VM 104',
	created_at: '2026-11-08T17:00:00Z',
};

describe('inboxState', () => {
	it('a run in flight wins over the status', () => {
		expect(inboxState({ status: 'approved' }, { status: 'running' })).toBe('running');
	});
	it('names every state a change waits or ends in', () => {
		expect(inboxState({ status: 'proposed' }, null)).toBe('review');
		expect(inboxState({ status: 'approved' }, null)).toBe('ready');
		expect(inboxState({ status: 'applied' }, null)).toBe('applied');
		expect(inboxState({ status: 'failed' }, null)).toBe('failed');
		expect(
			inboxState(
				{
					status: 'failed',
					partial_apply: { steps_done: ['a'], steps_failed: ['b'], steps_not_run: [], error: 'x' },
				},
				null,
			),
		).toBe('partial');
		expect(inboxState({ status: 'withdrawn' }, null)).toBe('withdrawn');
	});
});

describe('the sentences', () => {
	it('says when a run started, in local time', () => {
		const at = new Date(2026, 10, 8, 18, 28).toISOString();
		expect(runningLine('Back up VM 104', at)).toBe('Running: Back up VM 104, started 18:28');
		expect(runningLine('Back up VM 104', undefined)).toBe('Running: Back up VM 104');
		expect(hhmm('not a date')).toBe('');
	});

	it('a partial apply is stated as the server named it, capitalised', () => {
		const fm: Artifact = {
			...base,
			status: 'failed',
			failure_reason: 'partly applied: step a done; step b failed: 500',
			partial_apply: { steps_done: ['a'], steps_failed: ['b'], steps_not_run: [], error: '500' },
		};
		expect(outcomeSentence(fm, null)).toBe('Partly applied: step a done; step b failed: 500.');
	});

	it('an unknown outcome is never called a failure', () => {
		const text = outcomeSentence({ ...base, status: 'approved' }, {
			id: 't',
			artifact_id: 'a',
			action: 'apply',
			status: 'unknown',
			created_at: '',
			finished_at: null,
			error: 'outcome unknown: PVE task X is still running',
			result: {},
		});
		expect(text).toMatch(/^Outcome unknown/);
		expect(text).not.toMatch(/fail/i);
	});

	it('a withdrawal says it never ran', () => {
		const text = outcomeSentence(
			{ ...base, status: 'withdrawn', withdrawn_by: { user: 'owner', reason: 'wrong VM' } },
			null,
		);
		expect(text).toBe('Withdrawn by owner (wrong VM). It was never run.');
	});
});

describe('recentlyFinished', () => {
	it('keeps the last day, newest first, and drops what has no finish time', () => {
		const now = Date.parse('2026-11-08T20:00:00Z');
		const items: Artifact[] = [
			{ ...base, id: 'old', status: 'applied', applied_at: '2026-11-06T20:00:00Z' },
			{ ...base, id: 'new', status: 'applied', applied_at: '2026-11-08T19:00:00Z' },
			{ ...base, id: 'mid', status: 'failed', failed_at: '2026-11-08T10:00:00Z' },
			{ ...base, id: 'none', status: 'applied' },
		];
		expect(recentlyFinished(items, now).map((a) => a.id)).toEqual(['new', 'mid']);
	});
});
