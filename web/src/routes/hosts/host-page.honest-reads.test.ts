import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import HostPage from './[id]/+page.svelte';
import { api } from '$lib/api';

/**
 * A read that FAILED is not a read that found nothing (#701).
 *
 * The host page turned every failed read into a confident empty state: a
 * metrics fetch error said "The agent has not reported metrics yet", a failed
 * journal read said "Nothing in the journal names this host yet", and a failed
 * doc read - or a doc whose `sources.artifact_history.read` is false - said "No
 * artifact has targeted this host yet". The operator cannot tell "nothing
 * happened" from "I could not look", which on an ops console are opposite
 * conclusions. Drift on the same page already did this right.
 *
 * Each tab must say it could not read, with the reason, and must NOT print the
 * empty-state sentence. The honest other arm (a real empty answer still reads
 * as empty) is kept so "always say unknown" cannot pass.
 *
 * Teeth: restore the bare `catch { latest = [] }`, the `: []` fallback for the
 * journal, or ignore `doc.sources`, and the matching test fails.
 */
const getHost = api.getHost as ReturnType<typeof vi.fn>;
const getHostDoc = api.getHostDoc as ReturnType<typeof vi.fn>;
const getHostLatest = api.getHostLatest as ReturnType<typeof vi.fn>;
const listAudit = api.listAudit as ReturnType<typeof vi.fn>;

const HOST = {
	id: 'h-web',
	hostname: 'web1',
	status: 'online',
	role: 'service',
	host_type: 'vm',
	source: 'agent',
	agent_id: 'agent-1',
	services: [],
	agent: { agent_id: 'agent-1', connected: true, version: 'v2.9.0' },
};

function doc(extra: Record<string, unknown> = {}) {
	return {
		target: 'web1',
		hosts: [HOST],
		services: [],
		kb_entries: [],
		artifact_history: [],
		sources: {
			inventory: { read: true },
			knowledge_base: { read: true },
			artifact_history: { read: true },
		},
		...extra,
	};
}

function at(tab: string): void {
	setPage(`/ui/hosts/h-web?tab=${tab}`, { id: 'h-web' });
}

describe('Host page: failed reads say so (#701)', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		getHost.mockResolvedValue(HOST);
		getHostDoc.mockResolvedValue(doc());
		getHostLatest.mockResolvedValue({ hostname: 'web1', metrics: [] });
		listAudit.mockResolvedValue({ items: [], total: 0 });
	});

	it('metrics: a failed read is not "not reported yet"', async () => {
		at('metrics');
		getHostLatest.mockRejectedValue(new Error('503 metrics storage not available'));
		render(HostPage);
		await waitFor(() => expect(screen.getByText(/could not read this host.s metrics/i)).toBeTruthy());
		expect(screen.getByText(/metrics storage not available/)).toBeTruthy();
		expect(screen.queryByText(/has not reported metrics yet/i)).toBeNull();
	});

	it('metrics: a real empty answer still reads as not reported yet', async () => {
		at('metrics');
		render(HostPage);
		await waitFor(() => expect(screen.getByText(/has not reported metrics yet/i)).toBeTruthy());
		expect(screen.queryByText(/could not read/i)).toBeNull();
	});

	it('activity: a failed journal read is not an empty journal', async () => {
		at('activity');
		listAudit.mockRejectedValue(new Error('500 audit read failed'));
		render(HostPage);
		await waitFor(() => expect(screen.getByText(/could not read the journal/i)).toBeTruthy());
		expect(screen.queryByText(/nothing in the journal names this host/i)).toBeNull();
	});

	it('changes: a failed doc read is not "no artifact has targeted this host"', async () => {
		at('changes');
		getHostDoc.mockRejectedValue(new Error('500 doc failed'));
		render(HostPage);
		await waitFor(() => expect(screen.getByText(/could not read this host.s changes/i)).toBeTruthy());
		expect(screen.queryByText(/no artifact has targeted this host/i)).toBeNull();
	});

	it('changes: honours sources.artifact_history.read = false', async () => {
		at('changes');
		getHostDoc.mockResolvedValue(
			doc({
				sources: {
					inventory: { read: true },
					knowledge_base: { read: true },
					artifact_history: { read: false, error: 'database is locked' },
				},
			})
		);
		render(HostPage);
		await waitFor(() => expect(screen.getByText(/could not read this host.s changes/i)).toBeTruthy());
		expect(screen.getByText(/database is locked/)).toBeTruthy();
		expect(screen.queryByText(/no artifact has targeted this host/i)).toBeNull();
	});

	it('changes: a real empty history still reads as none', async () => {
		at('changes');
		render(HostPage);
		await waitFor(() => expect(screen.getByText(/no artifact has targeted this host/i)).toBeTruthy());
	});

	it('overview: unread notes and changes are named as attention, not silence', async () => {
		at('overview');
		getHostDoc.mockResolvedValue(
			doc({
				sources: {
					inventory: { read: true },
					knowledge_base: { read: false, error: 'embedding index offline' },
					artifact_history: { read: false, error: 'database is locked' },
				},
			})
		);
		render(HostPage);
		await waitFor(() => expect(screen.getByText(/could not read this host.s changes/i)).toBeTruthy());
		expect(screen.getByText(/could not read the notes/i)).toBeTruthy();
		expect(screen.queryByText(/nothing needs you on this host/i)).toBeNull();
	});
});
