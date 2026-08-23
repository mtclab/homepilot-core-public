import { render, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the components, or they resolve the real $lib/api.
import '../lib/test-mocks';
import { goto } from '../lib/test-mocks';

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

/**
 * THE S4 GATE (#514): every pre-move URL redirects. A bookmark, a webhook
 * body, a link in an old journal entry - none of them may 404 because the nav
 * was reorganised. Each old route renders a Redirect that goto()s its
 * successor, replaceState so Back does not trap the user in a loop.
 */
const CASES: Array<[string, unknown, string]> = [
	['/inventory', InventoryRedirect, '/ui/hosts'],
	['/agents', AgentsRedirect, '/ui/hosts'],
	['/tokens', TokensRedirect, '/ui/settings'],
	['/artifacts', ArtifactsRedirect, '/ui/changes'],
	['/review', ReviewRedirect, '/ui/changes/review'],
	['/drift', DriftRedirect, '/ui/changes/drift'],
	['/tasks', TasksRedirect, '/ui/records/tasks'],
	['/journal', JournalRedirect, '/ui/records/journal'],
	['/kb', KbRedirect, '/ui/records/kb'],
	['/records', RecordsIndex, '/ui/records/tasks'],
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
});
