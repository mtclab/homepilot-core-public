import { cleanup, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '../test-mocks';
import HostNetwork from './HostNetwork.svelte';
import Hosts from '../../routes/hosts/+page.svelte';
import { api, type HostNetwork as HostNetworkData } from '$lib/api';
import { addressCell, sourceLabel, wireText } from '$lib/addresses';

/**
 * Lane B2: where an operator SEES addresses - the host's Network tab and the
 * Hosts list.
 *
 * The journey: two guests are configured onto one address. The host page must
 * list every address with where it came from and how old that is, name the
 * other machine on the clash with a door to it, and never say "no address" when
 * the read failed. The Hosts list must show the address and mark the clash.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - drop the clash section and "names the other machine" fails;
 *   - render the unread branch as the empty one and the unread case fails;
 *   - put h.ip_address back in the Hosts cell and the list case fails on the
 *     second address and the clash marker.
 */
const getHostNetwork = api.getHostNetwork as ReturnType<typeof vi.fn>;
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

const CLASHING: HostNetworkData = {
	host_id: 'h1',
	addresses: [
		{
			address: '192.0.2.10',
			prefixlen: 24,
			iface: 'net0',
			mac: 'BC:24:11:00:00:01',
			bridge: 'vmbr0',
			vlan: null,
			source: 'qemu-ipconfig',
			observed_at: minutesAgo(3),
		},
		{
			address: '198.51.100.4',
			prefixlen: 24,
			iface: 'eth1',
			mac: 'BC:24:11:00:00:02',
			bridge: null,
			vlan: null,
			source: 'guest-agent',
			observed_at: minutesAgo(90),
		},
	],
	conflicts: [
		{
			address: '192.0.2.10',
			kind: 'configured',
			severity: 'critical',
			summary:
				'192.0.2.10 is configured on 2 machines: web-01 (VM 101, running) and db-01 (VM 102, stopped)',
			remedy: 'Whichever starts second takes the address over on the LAN.',
			host_ids: ['h1', 'h2'],
			holders: [],
		},
	],
};

describe('Host page Network tab (lane B2)', () => {
	afterEach(() => cleanup());
	beforeEach(() => vi.clearAllMocks());

	it('lists every address with its source and age, and names the other machine on a clash', async () => {
		getHostNetwork.mockResolvedValue(CLASHING);
		render(HostNetwork, { hostId: 'h1' });

		const row = await screen.findByText(/192\.0\.2\.10\/24/);
		const tr = within(row.closest('tr') as HTMLElement);
		expect(tr.getByText('net0 on vmbr0')).toBeInTheDocument();
		expect(tr.getByText('cloud-init')).toBeInTheDocument();
		expect(tr.getByText('clash')).toHaveClass('badge-critical');

		const observed = within(screen.getByText(/198\.51\.100\.4/).closest('tr') as HTMLElement);
		expect(observed.getByText('guest agent')).toBeInTheDocument();
		expect(observed.getByText(/seen 1h ago/)).toBeInTheDocument();

		const clash = screen.getByRole('link', { name: /configured on 2 machines/i });
		expect(clash.textContent).toContain('db-01 (VM 102, stopped)');
		expect(clash).toHaveAttribute('href', '/ui/hosts/h2?tab=network');
		expect(getHostNetwork).toHaveBeenCalledWith('h1');
	});

	it('never says "no address" when the read failed', async () => {
		getHostNetwork.mockRejectedValue(new Error('500 boom'));
		render(HostNetwork, { hostId: 'h1' });
		expect(await screen.findByText(/could not read this host's addresses/i)).toBeInTheDocument();
		expect(screen.queryByText(/no address is known/i)).not.toBeInTheDocument();
	});

	it('says plainly when no address is known', async () => {
		getHostNetwork.mockResolvedValue({ host_id: 'h1', addresses: [], conflicts: [] });
		render(HostNetwork, { hostId: 'h1' });
		expect(await screen.findByText(/no address is known for this host yet/i)).toBeInTheDocument();
		expect(screen.queryByText(/address clashes/i)).not.toBeInTheDocument();
	});
});

describe('Hosts list addresses (lane B2)', () => {
	afterEach(() => cleanup());
	beforeEach(() => vi.clearAllMocks());

	it('shows every address a host holds and marks the clashing one', async () => {
		listInventory.mockResolvedValue({
			items: [
				{
					id: 'h1',
					hostname: 'web-01',
					status: 'online',
					source: 'discovered',
					import_state: 'adopted',
					ip_address: '192.0.2.10',
					addresses: [
						{ address: '192.0.2.10', sources: ['qemu-ipconfig'], conflict: true },
						{ address: '198.51.100.4', sources: ['guest-agent'], conflict: false },
					],
				},
			],
			total: 1,
		});
		render(Hosts);
		const clash = await screen.findByText('192.0.2.10 (!)');
		expect(clash).toHaveClass('text-danger');
		expect(clash.textContent).toContain('address clash');
		await waitFor(() => expect(screen.getByText('198.51.100.4')).toBeInTheDocument());
	});
});

describe('address wording', () => {
	it('names sources the way an operator says them', () => {
		expect(sourceLabel('qemu-ipconfig')).toBe('cloud-init');
		expect(sourceLabel('lxc-net')).toBe('container config');
		expect(sourceLabel('arp')).toBe('answered ARP');
	});

	it('says where an address sits', () => {
		expect(wireText({ iface: 'net0', bridge: 'vmbr0', vlan: 20 })).toBe('net0 on vmbr0 (VLAN 20)');
		expect(wireText({ iface: 'vmbr0', bridge: 'vmbr0', vlan: null })).toBe('vmbr0');
		expect(wireText({ iface: 'eth0', bridge: null, vlan: null })).toBe('eth0');
	});

	it('marks a clashing address in the list cell', () => {
		expect(addressCell([{ address: '192.0.2.1', sources: ['manual'], conflict: true }])).toEqual([
			{ text: '192.0.2.1 (!)', conflict: true },
		]);
		expect(addressCell(undefined)).toEqual([]);
	});
});
