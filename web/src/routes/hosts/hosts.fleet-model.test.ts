import { render, screen, waitFor, fireEvent, within, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import Hosts from './+page.svelte';
import HostPage from './[id]/+page.svelte';
import { api } from '$lib/api';

/**
 * The fleet knows what Proxmox knows (lane B1, review 02 steps 1-3).
 *
 * THE JOURNEYS, as an operator walks them on the Hosts page:
 *  - the two Proxmox templates are not in the list, and the page says so
 *    ("2 hidden"); asking for them shows them as templates, never as an
 *    offline guest to adopt;
 *  - "which VMs are dev?" is a VMID and an env on every row and one filter;
 *  - each guest opens in Proxmox from its row menu;
 *  - HomePilot proposes which row is its own host, an admin confirms, and the
 *    row carries the mark.
 *
 * Teeth: request templates by default and the hidden-count test fails; drop the
 * env/VMID columns and the dev test fails; drop the Proxmox link and its test
 * fails; mark on a proposal without the confirm call and the banner test fails.
 */
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;
const getSelfGuest = api.getSelfGuest as ReturnType<typeof vi.fn>;
const confirmSelfGuest = api.confirmSelfGuest as ReturnType<typeof vi.fn>;
const getHost = api.getHost as ReturnType<typeof vi.fn>;
const updateHost = api.updateHost as ReturnType<typeof vi.fn>;

function host(over: { id: string } & Record<string, unknown>) {
	return {
		hostname: over.id,
		role: 'guest',
		status: 'offline',
		host_type: 'qemu',
		source: 'discovered',
		import_state: 'pending',
		absent_since: null,
		is_template: 0,
		is_self: 0,
		env: null,
		env_source: null,
		proxmox_url: null,
		...over,
	};
}

const SERVER = host({
	id: 'hp-test-server',
	proxmox_id: 100,
	status: 'online',
	import_state: 'adopted',
	source: 'imported',
	is_self: 1,
	env: 'infra',
	env_source: 'user',
	proxmox_url: 'https://pve.example.com:8006/#v1:0:=qemu%2F100',
});
const DEV = host({
	id: 'live-c',
	proxmox_id: 103,
	env: 'dev',
	env_source: 'pve_tag',
	proxmox_url: 'https://pve.example.com:8006/#v1:0:=qemu%2F103',
});
const TEMPLATE = host({ id: 'ubuntu-cloudinit', proxmox_id: 9000, is_template: 1, env: 'template' });

describe('Hosts: the fleet model (lane B1)', () => {
	afterEach(() => cleanup());

	beforeEach(() => {
		vi.clearAllMocks();
		listInventory.mockImplementation(async (params: { templates?: string; env?: string }) => {
			let items = [SERVER, DEV, TEMPLATE];
			if (params.templates === 'exclude') items = items.filter((h) => !h.is_template);
			if (params.env) items = items.filter((h) => h.env === params.env);
			return {
				items: items.map((h) => ({ ...h })),
				total: items.length,
				templates_hidden: params.templates === 'exclude' ? 1 : 0,
			};
		});
	});

	it('leaves templates out by default, says how many, and shows them on request as templates', async () => {
		render(Hosts);
		await waitFor(() => expect(screen.getByText('live-c')).toBeTruthy());

		expect(listInventory.mock.calls[0][0].templates).toBe('exclude');
		expect(screen.queryByText('ubuntu-cloudinit')).toBeNull();
		expect(screen.getByText('(1 hidden)')).toBeTruthy();

		await fireEvent.click(screen.getByRole('checkbox', { name: /Show templates/ }));
		await waitFor(() => expect(screen.getByText('ubuntu-cloudinit')).toBeTruthy());

		// In its own group, chipped "template" - not "offline", not Discovered.
		const group = screen.getByText('ubuntu-cloudinit').closest('tbody') as HTMLElement;
		expect(group.getAttribute('data-group')).toBe('templates');
		expect(within(group).getAllByText('template').length).toBeGreaterThan(0);
		expect(within(group).queryByText('offline')).toBeNull();
		// And there is nothing to adopt in its menu.
		await fireEvent.click(within(group).getByRole('button', { name: /Actions for ubuntu-cloudinit/ }));
		expect(within(group).queryByRole('menuitem', { name: 'Adopt' })).toBeNull();
	});

	it('answers "which VMs are dev" with a VMID, an env and one filter', async () => {
		render(Hosts);
		await waitFor(() => expect(screen.getByText('live-c')).toBeTruthy());

		const row = screen.getByText('live-c').closest('tr') as HTMLElement;
		expect(within(row).getByText('103')).toBeTruthy();
		expect(within(row).getByText('dev')).toBeTruthy();

		await fireEvent.change(screen.getByRole('combobox', { name: 'Filter by env' }), {
			target: { value: 'dev' },
		});
		await waitFor(() => expect(screen.queryByText('hp-test-server')).toBeNull());
		expect(listInventory.mock.calls.at(-1)?.[0].env).toBe('dev');
		expect(screen.getByText('live-c')).toBeTruthy();
	});

	it('opens a guest in Proxmox from its row menu', async () => {
		render(Hosts);
		await waitFor(() => expect(screen.getByText('live-c')).toBeTruthy());

		const row = screen.getByText('live-c').closest('tr') as HTMLElement;
		await fireEvent.click(within(row).getByRole('button', { name: /Actions for live-c/ }));
		const link = within(row).getByRole('menuitem', { name: /Open in Proxmox/ });
		expect(link.getAttribute('href')).toBe('https://pve.example.com:8006/#v1:0:=qemu%2F103');
		expect(link.getAttribute('target')).toBe('_blank');
	});

	it("marks HomePilot's own host on its row", async () => {
		render(Hosts);
		await waitFor(() => expect(screen.getByText('hp-test-server')).toBeTruthy());

		const own = screen.getByText('hp-test-server').closest('tr') as HTMLElement;
		expect(within(own).getByText('HomePilot')).toBeTruthy();
		const other = screen.getByText('live-c').closest('tr') as HTMLElement;
		expect(within(other).queryByText('HomePilot')).toBeNull();
	});

	it('proposes its own host with the evidence, and marks it only when an admin confirms', async () => {
		const proposal = {
			host_id: 'live-c',
			hostname: 'live-c',
			proxmox_id: 103,
			node: 'pve',
			address: '10.0.0.100',
			evidence: 'ipconfig0 = 10.0.0.100, which is agent hub advertise address',
		};
		getSelfGuest.mockResolvedValue({
			confirmed: null,
			proposal,
			candidates: [proposal],
			control_plane_addresses: [],
			dismissed_host_id: null,
		});
		confirmSelfGuest.mockResolvedValue({
			confirmed: { ...DEV, is_self: 1 },
			proposal: null,
			candidates: [],
			control_plane_addresses: [],
			dismissed_host_id: null,
		});
		render(Hosts);

		const banner = await screen.findByRole('region', { name: "HomePilot's own host" });
		expect(within(banner).getByText(/ipconfig0 = 10.0.0.100/)).toBeTruthy();
		expect(confirmSelfGuest).not.toHaveBeenCalled();

		await fireEvent.click(within(banner).getByRole('button', { name: /Yes, this is HomePilot's host/ }));

		await waitFor(() => expect(confirmSelfGuest).toHaveBeenCalledWith('live-c'));
		await waitFor(() =>
			expect(screen.queryByRole('region', { name: "HomePilot's own host" })).toBeNull(),
		);
	});
});

describe('Host page: VMID, env and the Proxmox link (lane B1)', () => {
	afterEach(() => cleanup());

	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/hosts/live-c', { id: 'live-c' });
		getHost.mockResolvedValue({ ...DEV, pool: 'HomePilot', pve_tags: 'web;hp-env-dev', services: [] });
		updateHost.mockResolvedValue({});
	});

	it('shows where the env came from, pins an operator value and can hand it back', async () => {
		render(HostPage);
		await waitFor(() => expect(screen.getByRole('heading', { name: 'live-c' })).toBeTruthy());

		expect(screen.getByText('from the Proxmox tag')).toBeTruthy();
		expect(screen.getByText('103')).toBeTruthy();
		expect(screen.getByText('hp-env-dev')).toBeTruthy();
		expect(screen.getByRole('link', { name: /Open in Proxmox/ }).getAttribute('href')).toBe(
			'https://pve.example.com:8006/#v1:0:=qemu%2F103',
		);

		const envRow = screen.getByText('Env').nextElementSibling as HTMLElement;
		await fireEvent.click(within(envRow).getByRole('button', { name: 'Edit' }));
		await fireEvent.change(within(envRow).getByRole('combobox', { name: 'Env' }), {
			target: { value: 'prod' },
		});
		// The reload after saving reads the operator-set value back.
		getHost.mockResolvedValue({ ...DEV, env: 'prod', env_source: 'user', services: [] });
		await fireEvent.click(within(envRow).getByRole('button', { name: 'Save' }));
		await waitFor(() => expect(updateHost).toHaveBeenCalledWith('live-c', { env: 'prod' }));

		// Now operator-set: the page says so and offers to hand it back to Proxmox.
		await waitFor(() => expect(screen.getByText('set here')).toBeTruthy());
		await fireEvent.click(screen.getByRole('button', { name: "Use Proxmox's" }));
		await waitFor(() => expect(updateHost).toHaveBeenLastCalledWith('live-c', { env: '' }));
	});
});
