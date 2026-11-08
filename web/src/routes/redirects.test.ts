import { render, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the components, or they resolve the real $lib/api.
import '../lib/test-mocks';
import { goto, setPage } from '../lib/test-mocks';

import InventoryRedirect from './inventory/+page.svelte';
import AgentsRedirect from './agents/+page.svelte';
import TokensRedirect from './tokens/+page.svelte';
import ArtifactsRedirect from './artifacts/+page.svelte';
import ReviewRedirect from './review/+page.svelte';
import DriftRedirect from './drift/+page.svelte';
import TasksRedirect from './tasks/+page.svelte';
import JournalRedirect from './journal/+page.svelte';
import KbRedirect from './kb/+page.svelte';
import RecordsIndex from './records/+page.svelte';
import MonitoringIndex from './monitoring/+page.svelte';
import SettingsPage from './settings/+page.svelte';

/**
 * THE S4 GATE (#514): every pre-move URL redirects. A bookmark, a webhook
 * body, a link in an old journal entry - none of them may 404 because the nav
 * was reorganised. Each old route renders a Redirect that goto()s its
 * successor, replaceState so Back does not trap the user in a loop.
 */
const CASES: Array<[string, unknown, string]> = [
	['/inventory', InventoryRedirect, '/ui/hosts'],
	// UI slice 1: the agent chores are Hosts > Agents, the tokens are
	// Assistants. Both used to land on a page that no longer holds them.
	['/agents', AgentsRedirect, '/ui/hosts/agents'],
	['/tokens', TokensRedirect, '/ui/assistants'],
	['/artifacts', ArtifactsRedirect, '/ui/changes'],
	['/review', ReviewRedirect, '/ui/changes/review'],
	['/drift', DriftRedirect, '/ui/changes/drift'],
	['/tasks', TasksRedirect, '/ui/records/tasks'],
	['/journal', JournalRedirect, '/ui/records/journal'],
	['/kb', KbRedirect, '/ui/records/kb'],
	['/records', RecordsIndex, '/ui/records/tasks'],
	// Monitoring's index is its first view, the order of urgency.
	['/monitoring', MonitoringIndex, '/ui/monitoring/alerts'],
];

describe('Every pre-move URL redirects (#514 S4)', () => {
	beforeEach(() => {
		(goto as ReturnType<typeof vi.fn>).mockClear();
	});

	for (const [from, component, to] of CASES) {
		it(`${from} → ${to}`, async () => {
			render(component as never);
			await waitFor(() => expect(goto).toHaveBeenCalled());
			const [target, opts] = (goto as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(String(target).split('?')[0]).toBe(to);
			expect(opts).toMatchObject({ replaceState: true });
		});
	}

	/**
	 * A redirect keeps the sender's intent: the query (a search, a filter) and
	 * the fragment (where on the page) travel with it. Teeth: drop either from
	 * Redirect.svelte and this fails.
	 */
	it('carries the query and the fragment to the new home', async () => {
		window.history.replaceState({}, '', '/ui/agents?q=web01#join');
		try {
			render(AgentsRedirect as never);
			await waitFor(() => expect(goto).toHaveBeenCalled());
			const [target] = (goto as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(String(target)).toBe('/ui/hosts/agents?q=web01#join');
		} finally {
			window.history.replaceState({}, '', '/');
		}
	});
});

/**
 * UI slice 1: the sectors that left Settings. Every address that named one -
 * by `?tab=` (the current form) or by fragment (the pre-tab form) - lands on
 * the page that has it now, and keeps the rest of its query.
 */
const SETTINGS_CASES: Array<[string, string]> = [
	['/ui/settings?tab=monitoring', '/ui/monitoring/rules'],
	['/ui/settings#monitoring', '/ui/monitoring/rules'],
	['/ui/settings#alerts', '/ui/monitoring/rules'],
	['/ui/settings?tab=guests', '/ui/guests'],
	['/ui/settings#guests', '/ui/guests'],
	['/ui/settings?tab=tokens', '/ui/assistants'],
	['/ui/settings#tokens', '/ui/assistants'],
	['/ui/settings?tab=monitoring&rule=r1', '/ui/monitoring/rules?rule=r1'],
];

describe('Every Settings sector that moved redirects to its new page (UI slice 1)', () => {
	beforeEach(() => {
		(goto as ReturnType<typeof vi.fn>).mockClear();
	});

	for (const [from, to] of SETTINGS_CASES) {
		it(`${from} → ${to}`, async () => {
			setPage(from);
			render(SettingsPage as never);
			await waitFor(() => expect(goto).toHaveBeenCalled());
			const [target, opts] = (goto as ReturnType<typeof vi.fn>).mock.calls[0];
			expect(String(target)).toBe(to);
			expect(opts).toMatchObject({ replaceState: true });
		});
	}
});
