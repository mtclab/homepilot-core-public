import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '../test-mocks';
import AlertRules from './AlertRules.svelte';
import { api, type AlertRule, type AlertRuleCoverage } from '$lib/api';

/**
 * A fleet-wide percentage rule (#661).
 *
 * "Disk over 90%" is now writable - the agent reports disk.used_pct - but an
 * agent from before that does not, and a rule on it then watches fewer hosts
 * than the fleet. The number alone ("watching 2 hosts") reads as fine; the
 * server's sentence names the hosts and the fix, and the console must show it.
 *
 * Teeth: drop the coverage-note span and the first case fails; take the ratios
 * out of RULE_METRICS and the picker case fails.
 */
const listAlertRules = api.listAlertRules as ReturnType<typeof vi.fn>;

const RULE: AlertRule & AlertRuleCoverage = {
	id: 'r-pct',
	name: 'Disk over 90%',
	kind: 'threshold',
	host_filter: '*',
	metric: 'disk.used_pct',
	comparison: 'gt',
	threshold: 90,
	for_seconds: 600,
	enabled: 1,
	created_at: '2026-11-18T00:00:00Z',
	updated_at: '2026-11-18T00:00:00Z',
	hosts_matched: 2,
	last_eval_at: '2026-11-18T00:01:00Z',
	hosts_without_metric: 1,
	hosts_without_metric_names: ['old01'],
	coverage_note:
		'1 host does not report disk.used_pct yet - upgrade its agent (old01). Until then this rule does not watch it.'
};

describe('Alert rules on the ratios (#661)', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('says which hosts a rule cannot see yet, and why', async () => {
		listAlertRules.mockResolvedValue({ items: [RULE], total: 1 });
		const { container } = render(AlertRules);
		await waitFor(() => expect(screen.getByText('Disk over 90%')).toBeTruthy());
		expect(screen.getByText(/watching 2\s+hosts/)).toBeTruthy();
		const note = container.querySelector('[data-rule-missing-metric]');
		expect(note?.textContent).toBe(RULE.coverage_note);
	});

	it('shows no note when every reporting host reports the metric', async () => {
		listAlertRules.mockResolvedValue({
			items: [{ ...RULE, hosts_without_metric: 0, hosts_without_metric_names: [], coverage_note: null }],
			total: 1
		});
		const { container } = render(AlertRules);
		await waitFor(() => expect(screen.getByText('Disk over 90%')).toBeTruthy());
		expect(container.querySelector('[data-rule-missing-metric]')).toBeNull();
	});

	it('offers the percentages in the metric picker', async () => {
		listAlertRules.mockResolvedValue({ items: [], total: 0 });
		render(AlertRules);
		for (const label of ['Disk used', 'Memory used', 'Load 1m per core']) {
			expect(await screen.findByRole('option', { name: label })).toBeTruthy();
		}
		expect(screen.queryByRole('option', { name: 'CPU cores' })).toBeNull();
	});
});
