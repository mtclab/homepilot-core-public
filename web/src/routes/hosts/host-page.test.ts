import { render, screen, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import HostPage from './[id]/+page.svelte';
import { api } from '$lib/api';

/**
 * The host page (#514 S2): one machine, everything HomePilot knows about it.
 *
 * The Agents-tab expansion this replaces rendered the machine as a raw dump -
 * the same facts three ways (rows + JSON blobs + charts), values floating at
 * the far table edge, `{"free_gb":73.41,...}` printed verbatim. These gates
 * forbid that class: the facts render as FORMATTED values, raw JSON never
 * reaches the DOM, and the operator questions (is the channel live, why was it
 * refused, what changed here) are answered on this one page.
 *
 * Teeth: render the agent's system_info/state blobs anywhere and the raw-JSON
 * gate fails; drop the last_error row and the refusal gate fails.
 */
const getHost = api.getHost as ReturnType<typeof vi.fn>;
const getHostDoc = api.getHostDoc as ReturnType<typeof vi.fn>;
const getHostLatest = api.getHostLatest as ReturnType<typeof vi.fn>;
const getHostSeries = api.getHostSeries as ReturnType<typeof vi.fn>;
const listAudit = api.listAudit as ReturnType<typeof vi.fn>;

// Shaped exactly like the live instance that produced the complaint: nested
// JSON in system_info, duplicate facts, load/memory/disk blobs.
const HOST = {
	id: 'h-llm',
	hostname: 'llm',
	status: 'online',
	role: 'service',
	host_type: 'physical',
	source: 'agent',
	os_info: 'Linux 7.0.0-30-generic',
	cpu_cores: 20,
	memory_mb: 58961,
	ip_address: '10.0.0.1',
	agent_id: 'agent-74ab',
	services: [],
	agent: {
		agent_id: 'agent-74ab',
		connected: true,
		version: 'v2.9.0',
		arch: 'amd64',
		connected_at: '2026-08-23T13:00:49Z',
		last_heartbeat: '2026-08-23T13:02:49Z',
		last_error: null,
		credential_set_at: '2026-08-23T13:00:49Z',
		revoked_at: null,
	},
};

const METRICS = [
	{ metric: 'load_1m', ts: 1787500000, value: 0.53 },
	{ metric: 'mem_free_gb', ts: 1787500000, value: 48.53 },
];

describe('Host page (#514 S2)', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		getHost.mockResolvedValue(HOST);
		getHostDoc.mockResolvedValue({
			target: 'llm',
			hosts: [HOST],
			services: [],
			kb_entries: [],
			artifact_history: [
				{ id: 'art-1', status: 'applied', kind: 'host-provision', intent: 'baseline packages', created_at: '2026-08-20T10:00:00Z' },
			],
		});
		getHostLatest.mockResolvedValue({ hostname: 'llm', metrics: METRICS });
		getHostSeries.mockResolvedValue({
			hostname: 'llm',
			metric: 'load_1m',
			since: 0,
			points: [
				{ ts: 1787499000, value: 1.1 },
				{ ts: 1787500000, value: 0.53 },
			],
		});
		listAudit.mockResolvedValue({
			items: [
				{ id: 1, timestamp: '2026-08-23T12:00:00Z', user_id: 'ui', source: 'ui', action: 'host_adopted', artifact_id: null, target_host: 'llm', target_service: null, command: null, exit_code: null },
			],
			total: 1,
		});
	});

	it('renders the machine, its facts formatted, and NO raw JSON anywhere', async () => {
		const { container } = render(HostPage);
		await waitFor(() => expect(screen.getByRole('heading', { name: 'llm' })).toBeTruthy());

		// Facts as values, not dumps: memory in GB, cores as a plain number.
		expect(screen.getByText('57.6 GB')).toBeTruthy();
		expect(screen.getByText('20')).toBeTruthy();
		expect(screen.getByText('Linux 7.0.0-30-generic')).toBeTruthy();

		// THE gate: nothing anywhere on this page may be a raw JSON blob. This is
		// what the Agents expansion did ({"free_gb":73.41,"total_gb":626.48}) and
		// the class this page exists to kill.
		expect(container.textContent).not.toMatch(/\{"[a-z_]+":/);
	});

	it('answers "is the channel live" and shows the agent, once', async () => {
		render(HostPage);
		await waitFor(() => expect(screen.getByText('agent connected')).toBeTruthy());
		expect(screen.getByText(/v2\.9\.0/)).toBeTruthy();
	});

	it('surfaces the refusal reason when the hub last rejected the agent', async () => {
		getHost.mockResolvedValue({
			...HOST,
			agent: {
				...HOST.agent,
				connected: false,
				last_error: 'credential revoked 2026-08-22; re-enroll with a fresh token',
				last_error_at: '2026-08-23T09:00:00Z',
				revoked_at: '2026-08-22T18:00:00Z',
			},
		});
		render(HostPage);
		await waitFor(() =>
			expect(screen.getByText(/credential revoked 2026-08-22/)).toBeTruthy()
		);
		// Revoked agents get Forget (they cannot be revoked twice); never both.
		expect(screen.getByRole('button', { name: 'Forget agent' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull();
	});

	it('shows what HomePilot has changed on this machine', async () => {
		render(HostPage);
		await waitFor(() => expect(screen.getByText('baseline packages')).toBeTruthy());
		expect(screen.getByText('host_adopted')).toBeTruthy();
	});

	it('a host with no agent explains the door that is open, not a dead end', async () => {
		getHost.mockResolvedValue({ ...HOST, agent_id: null, agent: undefined });
		render(HostPage);
		await waitFor(() =>
			expect(screen.getByText(/nothing reports metrics/i)).toBeTruthy()
		);
		expect(screen.getByText(/No agent is enrolled on this host/)).toBeTruthy();
	});

	it('destructive actions confirm in a dialog, not by swapping buttons', async () => {
		render(HostPage);
		await waitFor(() => expect(screen.getByRole('button', { name: 'Revoke' })).toBeTruthy());

		const revoke = screen.getByRole('button', { name: 'Revoke' });
		revoke.click();
		await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy());
		// The trigger is still there, un-swapped, behind the dialog.
		expect(screen.getByRole('button', { name: 'Revoke' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Confirm' })).toBeTruthy();
	});
});
