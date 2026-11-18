import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '../test-mocks';
import HostBuilt from './HostBuilt.svelte';
import * as apiModule from '$lib/api';
import { ApiError, type HostDescriptionView } from '$lib/api';

/**
 * #818: what an operator SEES on a host's "How it is built" tab.
 *
 * - the description, grouped: what runs (services with their program, compose
 *   projects, containers), how it is exposed (sockets, nginx vhosts with their
 *   targets, firewall), what it depends on (certificates with expiry, updates,
 *   time), what changed since the last one, and what the agent could not see;
 * - an agent too old to describe its host says so, and where to fix it;
 * - a failed read is never shown as "no description";
 * - Collect now asks the agent and shows the fresh answer;
 * - Export records the as-built note and names the one it supersedes - and is
 *   offered only to a writer.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - drop the `can_describe` branch and the old-agent case fails;
 *   - drop `{#if canWrite && d}` around Export and the read-only case fails;
 *   - render `unread` as the empty state and the failed-read case fails.
 */
const getHostDescription = apiModule.getHostDescription as unknown as ReturnType<typeof vi.fn>;
const collectHostDescription = apiModule.collectHostDescription as unknown as ReturnType<typeof vi.fn>;
const exportHostAsBuilt = apiModule.exportHostAsBuilt as unknown as ReturnType<typeof vi.fn>;

const description = {
	schema: 'hp.host-description/1',
	collected_at: '2026-11-18T12:00:00Z',
	agent: { version: '3.14.0', privileged: false, run_as: 'hp-agent' },
	host: { hostname: 'proxy-1', os: 'Ubuntu 24.04.1 LTS', kernel: '6.8.0' },
	listening: {
		status: 'partial',
		reason: 'the owning process of 1 socket(s) is not visible to the agent user',
		sockets: [{ proto: 'tcp', address: '0.0.0.0', port: 443, process: 'nginx' }],
	},
	units: {
		status: 'ok',
		units: [
			{ name: 'nginx.service', type: 'service', active: 'active', sub: 'running', enabled: 'enabled', programs: ['/usr/sbin/nginx'] },
			{ name: 'app.service', type: 'service', active: 'active', enabled: 'enabled', programs: ['/opt/app/bin/app'], custom: true },
			{ name: 'certbot.timer', type: 'timer', active: 'active', enabled: 'enabled' },
		],
	},
	docker: {
		status: 'ok',
		containers: [
			{ name: 'app-web-1', image: 'nginx:1.27', state: 'running', restart_count: 2, restart_policy: 'unless-stopped', ports: [{ container_port: 80, host_port: 8080, proto: 'tcp' }], mounts: [] },
		],
		compose_projects: [{ name: 'app', working_dir: '/opt/app', config_files: [], services: ['web'] }],
	},
	nginx: {
		status: 'ok',
		servers: [
			{ server_names: ['example.com'], listen: ['443 ssl'], tls: { verify_client: 'on' }, locations: [{ match: '/', proxy_pass: 'http://app_backend' }] },
		],
		upstreams: [],
	},
	certificates: {
		status: 'ok',
		certificates: [
			{ path: '/etc/ssl/certs/example.pem', status: 'ok', names: ['example.com'], not_after: '2026-12-18T12:00:00Z', days_left: 30 },
		],
		letsencrypt: [{ name: 'example.org', domains: ['example.org'], authenticator: 'webroot' }],
	},
	firewall: {
		ufw: { status: 'ok', enabled: true, rules: [{ action: 'allow', proto: 'tcp', port: '443', from: 'any' }] },
		nftables: { status: 'not_visible', reason: 'not visible to the agent: its user cannot read this (a privileged agent can)', tables: [] },
	},
	updates: { status: 'ok', pending: 7, security: 3, reboot_required: true },
	time: { status: 'ok', synchronized: true, timezone: 'UTC' },
};

function view(over: Partial<HostDescriptionView> = {}): HostDescriptionView {
	return {
		host_id: 'h1',
		hostname: 'proxy-1',
		description,
		collected_at: '2026-11-18T12:00:00Z',
		received_at: '2026-11-18T12:00:01Z',
		previous_collected_at: '2026-11-17T12:00:00Z',
		changes: [{ part: 'units', added: ['app.service (active, enabled)'], removed: [] }],
		last_error: null,
		last_error_at: null,
		as_built: null,
		agent: { connected: true, can_describe: true, version: '3.14.0' },
		...over,
	};
}

describe('How it is built (#818)', () => {
	beforeEach(() => {
		getHostDescription.mockReset();
		collectHostDescription.mockReset();
		exportHostAsBuilt.mockReset();
	});
	afterEach(() => cleanup());

	it('shows the host grouped: what runs, how it is exposed, what it depends on, what changed', async () => {
		getHostDescription.mockResolvedValue(view());
		render(HostBuilt, { hostId: 'h1', canWrite: true });
		await screen.findByText('What runs');
		expect(getHostDescription).toHaveBeenCalledWith('h1');
		expect(screen.getByText('/usr/sbin/nginx')).toBeInTheDocument();
		expect(screen.getByText(/\(custom\)/)).toBeInTheDocument();
		expect(screen.getByText(/Compose project/)).toHaveTextContent('Compose project app in /opt/app: web');
		expect(screen.getByText('8080->80/tcp')).toBeInTheDocument();
		expect(screen.getByText(/tcp 0\.0\.0\.0:443 \(nginx\)/)).toBeInTheDocument();
		expect(screen.getByText(/-> proxy to http:\/\/app_backend/)).toBeInTheDocument();
		expect(screen.getByText(/client certificates on/)).toBeInTheDocument();
		expect(screen.getByText(/\(30 days\)/)).toBeInTheDocument();
		expect(screen.getByText(/certbot renews example\.org via webroot/)).toBeInTheDocument();
		expect(screen.getByText(/7 pending update\(s\), 3 security; a reboot is required/)).toBeInTheDocument();
		expect(screen.getByText(/units: app\.service \(active, enabled\)/)).toBeInTheDocument();
		const hidden = screen.getByRole('heading', { name: 'Not visible to the agent' }).closest('section')!;
		expect(hidden).toHaveTextContent('nftables: not visible to the agent');
		expect(hidden).toHaveTextContent('Listening sockets: the owning process of 1 socket(s)');
	});

	it('says an agent is too old to describe its host, and where to upgrade it', async () => {
		getHostDescription.mockResolvedValue(
			view({ description: null, previous_collected_at: null, changes: [], agent: { connected: true, can_describe: false, version: '3.13.0' } }),
		);
		render(HostBuilt, { hostId: 'h1', canWrite: true });
		expect(await screen.findByText(/too old to describe its host/)).toHaveTextContent('3.13.0');
		expect(screen.getByText(/Upgrade it from the Agent tab/)).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Export to knowledge base' })).toBeNull();
	});

	it('a failed read is never "no description"', async () => {
		getHostDescription.mockRejectedValue(new ApiError(500, JSON.stringify({ detail: 'database is locked' })));
		render(HostBuilt, { hostId: 'h1' });
		expect(await screen.findByRole('alert')).toHaveTextContent('Could not read the description: database is locked');
		expect(screen.queryByText(/No description yet/)).toBeNull();
	});

	it('Collect now asks the agent and shows the fresh description', async () => {
		getHostDescription.mockResolvedValue(view({ description: null, previous_collected_at: null, changes: [] }));
		collectHostDescription.mockResolvedValue(view());
		render(HostBuilt, { hostId: 'h1' });
		await fireEvent.click(await screen.findByRole('button', { name: 'Collect now' }));
		await waitFor(() => expect(collectHostDescription).toHaveBeenCalledWith('h1'));
		expect(await screen.findByText('/usr/sbin/nginx')).toBeInTheDocument();
	});

	it('a refused collection shows the reason and keeps what was stored', async () => {
		getHostDescription.mockResolvedValue(view());
		collectHostDescription.mockRejectedValue(
			new ApiError(504, JSON.stringify({ detail: 'the agent on proxy-1 did not answer in time' })),
		);
		render(HostBuilt, { hostId: 'h1' });
		await fireEvent.click(await screen.findByRole('button', { name: 'Collect now' }));
		expect(await screen.findByRole('alert')).toHaveTextContent('did not answer in time');
		expect(screen.getByText('/usr/sbin/nginx')).toBeInTheDocument();
	});

	it('Export records the as-built note and names the one it supersedes', async () => {
		getHostDescription.mockResolvedValue(view());
		exportHostAsBuilt.mockResolvedValue({
			note_id: '2026-11-18-kb-as-built-proxy-1',
			superseded: '2026-11-17-kb-as-built-proxy-1',
			unchanged: false,
			indexed: true,
			doc_id: 4,
		});
		render(HostBuilt, { hostId: 'h1', canWrite: true });
		await fireEvent.click(await screen.findByRole('button', { name: 'Export to knowledge base' }));
		const status = await screen.findByRole('status');
		expect(exportHostAsBuilt).toHaveBeenCalledWith('h1');
		expect(status).toHaveTextContent('2026-11-18-kb-as-built-proxy-1, superseding 2026-11-17-kb-as-built-proxy-1');
	});

	it('a read-only operator is not offered the export', async () => {
		getHostDescription.mockResolvedValue(view());
		render(HostBuilt, { hostId: 'h1', canWrite: false });
		await screen.findByText('What runs');
		expect(screen.queryByRole('button', { name: 'Export to knowledge base' })).toBeNull();
		expect(screen.getByRole('button', { name: 'Collect now' })).toBeInTheDocument();
	});
});
