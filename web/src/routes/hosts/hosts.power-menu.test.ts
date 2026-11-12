import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import Hosts from './+page.svelte';
import { ApiError, api, sessionStore, type GuestPowerResult, type GuestPowerState, type Host } from '$lib/api';

/**
 * UI slice 5: power from the Hosts list. THE JOURNEYS, as the operator sees them:
 *
 * 1. a stopped guest, started from its row menu: the row says "Starting...
 *    following Proxmox task" with the old chip kept and marked pending, then
 *    reads `running` once the server re-reads it - and the row stays where it
 *    was (no jump between groups under the cursor);
 * 2. a Start refused for an address clash: the row names the address AND the
 *    machine holding it, and the chip stays `stopped`;
 * 3. a verb the live state rules out is offered disabled, with its reason;
 * 4. a forced Stop, and any verb on a protected guest, waits for the name typed
 *    back in the confirm dialog, and that name is what is sent;
 * 5. Console, `+ Add` and the phone card list.
 *
 * Teeth (each verified by reverting, evidence in the PR):
 *   - drop the row re-read after the action (`api.getHost` in runPower) and
 *     journey 1 fails: the chip never leaves `stopped`;
 *   - drop the holder branch in `refusalLine` and journey 2 fails;
 *   - render a disabled verb without its reason and journey 3 fails;
 *   - let Stop take the inline confirm instead of the dialog and journey 4 fails.
 */
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;
const getGuestPower = api.getGuestPower as ReturnType<typeof vi.fn>;
const powerGuest = api.powerGuest as ReturnType<typeof vi.fn>;
const getHost = api.getHost as ReturnType<typeof vi.fn>;

function guestRow(over: Partial<Host> = {}): Host {
	return {
		id: 'h-web',
		hostname: 'web-01',
		host_type: 'qemu',
		node: 'pve1',
		proxmox_id: 101,
		role: 'guest',
		status: 'offline',
		pve_status: 'stopped',
		source: 'imported',
		import_state: 'adopted',
		absent_since: null,
		env: 'dev',
		proxmox_url: 'https://pve.example.com:8006/#v1:0:=qemu%2F101',
		console_url:
			'https://pve.example.com:8006/?console=kvm&novnc=1&vmid=101&vmname=web-01&node=pve1&resize=off&cmd=',
		...over,
	};
}

function power(over: Partial<GuestPowerState> = {}): GuestPowerState {
	const stopped = (over.status ?? 'stopped') === 'stopped';
	return {
		host_id: 'h-web',
		hostname: 'web-01',
		vmid: 101,
		node: 'pve1',
		guest_type: 'qemu',
		is_self: false,
		protected: false,
		protected_reason: null,
		status: 'stopped',
		pve_name: 'web-01',
		name_matches: true,
		pending: [],
		actions: {
			start: { available: stopped, reason: stopped ? null : 'x', needs_typed_confirmation: false },
			shutdown: { available: !stopped, reason: stopped ? 'x' : null, needs_typed_confirmation: false },
			reboot: { available: !stopped, reason: stopped ? 'x' : null, needs_typed_confirmation: false },
			stop: { available: !stopped, reason: stopped ? 'x' : null, needs_typed_confirmation: false },
		},
		error: null,
		...over,
	};
}

function rowOf(name: string): HTMLElement {
	return screen.getByText(name).closest('tr') as HTMLElement;
}

async function openMenu(name: string): Promise<HTMLElement> {
	await fireEvent.click(within(rowOf(name)).getByRole('button', { name: `Actions for ${name}` }));
	const menu = within(rowOf(name)).getByRole('menu', { name: `Actions for ${name}` });
	// The live state is read when the menu opens.
	await waitFor(() =>
		expect(within(menu).queryByText('Reading its state from Proxmox')).toBeNull(),
	);
	return menu;
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	let reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

describe('Hosts row menu: power (UI slice 5)', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		sessionStore.set({ authenticated: true, capabilities: ['read', 'write', 'admin'] } as never);
		listInventory.mockResolvedValue({ items: [guestRow()], total: 1 });
		getGuestPower.mockResolvedValue(power());
	});

	it('starts a stopped guest from the row menu and the row goes to running', async () => {
		const sent = deferred<GuestPowerResult>();
		powerGuest.mockReturnValue(sent.promise);
		getHost.mockResolvedValue({ ...guestRow(), status: 'online', pve_status: 'running' });
		render(Hosts);
		await screen.findByText('web-01');
		const groupBefore = rowOf('web-01').closest('tbody')?.getAttribute('data-group');
		expect(within(rowOf('web-01')).getByText('stopped')).toBeTruthy();

		const menu = await openMenu('web-01');
		await fireEvent.click(within(menu).getByRole('menuitem', { name: /^Start/ }));
		// An ordinary start is the inline tier: the item turns into its confirm.
		await fireEvent.click(within(menu).getByRole('menuitem', { name: 'Confirm start' }));

		// While Proxmox works: honest, the old chip kept and marked pending.
		const line = await screen.findByText('Starting... following Proxmox task');
		expect(line.closest('[role="status"]')).toBeTruthy();
		expect(within(rowOf('web-01')).getByText('stopped')).toBeTruthy();
		expect(within(rowOf('web-01')).getByText('pending')).toBeTruthy();
		expect(powerGuest).toHaveBeenCalledWith('h-web', { action: 'start' });

		sent.resolve({
			action: 'start',
			outcome: 'ok',
			message: 'web-01 started and is running.',
			upid: 'UPID:pve1:1',
			status_after: 'running',
			next_actions: [],
			pending_after: null,
		});

		// Settled: the chip is the server's re-read, the line the server's verdict.
		await waitFor(() => expect(within(rowOf('web-01')).getByText('running')).toBeTruthy());
		expect(within(rowOf('web-01')).queryByText('stopped')).toBeNull();
		expect(within(rowOf('web-01')).queryByText('pending')).toBeNull();
		expect(screen.getByText('web-01 started and is running.')).toBeTruthy();
		expect(screen.queryByText('Starting... following Proxmox task')).toBeNull();
		expect(getHost).toHaveBeenCalledWith('h-web');
		// And it did not move under the cursor.
		expect(rowOf('web-01').closest('tbody')?.getAttribute('data-group')).toBe(groupBefore);
	});

	it('a Start refused for an address clash names the holder in the row', async () => {
		powerGuest.mockRejectedValue(
			new ApiError(
				409,
				JSON.stringify({
					detail: {
						code: 'address_clash',
						message:
							'cannot start web-01: 192.0.2.10 is held by relay (VMID 112, running). Nothing was sent.',
						holders: [
							{ address: '192.0.2.10', hostname: 'relay', vmid: 112, node: 'pve1', source: 'qemu-ipconfig' },
						],
					},
				}),
			),
		);
		getHost.mockResolvedValue(guestRow());
		render(Hosts);
		await screen.findByText('web-01');

		const menu = await openMenu('web-01');
		await fireEvent.click(within(menu).getByRole('menuitem', { name: /^Start/ }));
		await fireEvent.click(within(menu).getByRole('menuitem', { name: 'Confirm start' }));

		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toContain('Address 192.0.2.10 is used by relay (VMID 112)');
		expect(alert.textContent).toContain('Nothing was sent');
		// The line belongs to web-01's row, and the chip still says stopped.
		expect(alert.closest('tr')?.getAttribute('data-power-for')).toBe('h-web');
		expect(within(rowOf('web-01')).getByText('stopped')).toBeTruthy();

		await fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss' }));
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('a verb the live state rules out is disabled and says why', async () => {
		listInventory.mockResolvedValue({
			items: [guestRow({ status: 'online', pve_status: 'running' })],
			total: 1,
		});
		getGuestPower.mockResolvedValue(power({ status: 'running' }));
		render(Hosts);
		await screen.findByText('web-01');

		const menu = await openMenu('web-01');

		const start = within(menu).getByRole('menuitem', { name: /^Start/ });
		expect(start).toBeDisabled();
		expect(start.textContent).toContain('Already running');
		for (const name of [/^Shut down/, /^Reboot/, /^Stop/]) {
			expect(within(menu).getByRole('menuitem', { name })).toBeEnabled();
		}
	});

	it('a failed state read offers nothing and says it could not read', async () => {
		getGuestPower.mockRejectedValue(new Error('proxmox unreachable'));
		render(Hosts);
		await screen.findByText('web-01');

		const menu = await openMenu('web-01');

		const start = within(menu).getByRole('menuitem', { name: /^Start/ });
		expect(start).toBeDisabled();
		expect(start.textContent).toContain('Could not read its state from Proxmox');
	});

	it('a read-only session sees the verbs disabled, and no state is read', async () => {
		sessionStore.set({ authenticated: true, capabilities: ['read'] } as never);
		render(Hosts);
		await screen.findByText('web-01');

		const menu = await openMenu('web-01');

		expect(within(menu).getByRole('menuitem', { name: /^Start/ }).textContent).toContain(
			'Needs an admin session',
		);
		expect(getGuestPower).not.toHaveBeenCalled();
	});

	it('a forced Stop waits for the name typed back, and sends it', async () => {
		listInventory.mockResolvedValue({
			items: [guestRow({ status: 'online', pve_status: 'running' })],
			total: 1,
		});
		getGuestPower.mockResolvedValue(power({ status: 'running' }));
		powerGuest.mockResolvedValue({
			action: 'stop',
			outcome: 'ok',
			message: 'web-01 was stopped (forced).',
			upid: 'UPID:pve1:2',
			status_after: 'stopped',
			next_actions: [],
			pending_after: null,
		});
		getHost.mockResolvedValue({ ...guestRow(), status: 'offline', pve_status: 'stopped' });
		render(Hosts);
		await screen.findByText('web-01');

		const menu = await openMenu('web-01');
		await fireEvent.click(within(menu).getByRole('menuitem', { name: /^Stop/ }));

		const dialog = await screen.findByRole('dialog');
		expect(within(dialog).getByText('Stop web-01 (forced)')).toBeTruthy();
		expect(dialog.textContent).toContain('cuts power to VMID 101 on pve1');
		const go = within(dialog).getByRole('button', { name: 'Stop web-01' });
		expect(go).toBeDisabled();
		const input = within(dialog).getByRole('textbox', { name: 'Type web-01 to confirm' });
		await fireEvent.input(input, { target: { value: 'web-02' } });
		expect(go).toBeDisabled();
		expect(powerGuest).not.toHaveBeenCalled();

		await fireEvent.input(input, { target: { value: 'web-01' } });
		expect(go).toBeEnabled();
		await fireEvent.click(go);

		await waitFor(() =>
			expect(powerGuest).toHaveBeenCalledWith('h-web', { action: 'stop', confirm_name: 'web-01' }),
		);
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
		await screen.findByText('web-01 was stopped (forced).');
	});

	it('every verb on the machine HomePilot runs on takes the dialog, which says so', async () => {
		listInventory.mockResolvedValue({
			items: [guestRow({ status: 'online', pve_status: 'running', is_self: 1 })],
			total: 1,
		});
		getGuestPower.mockResolvedValue(power({ status: 'running', is_self: true, protected: true }));
		render(Hosts);
		await screen.findByText('web-01');

		const menu = await openMenu('web-01');
		await fireEvent.click(within(menu).getByRole('menuitem', { name: /^Shut down/ }));

		const dialog = await screen.findByRole('dialog');
		expect(dialog.textContent).toContain('This is the machine HomePilot runs on');
		expect(within(dialog).getByRole('button', { name: 'Shut down web-01' })).toBeDisabled();
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
		expect(powerGuest).not.toHaveBeenCalled();
	});

	it('opens the guest console from its row menu', async () => {
		render(Hosts);
		await screen.findByText('web-01');

		const menu = await openMenu('web-01');

		const link = within(menu).getByRole('menuitem', { name: /Console/ });
		expect(link.getAttribute('href')).toContain('console=kvm&novnc=1&vmid=101');
		expect(link.getAttribute('target')).toBe('_blank');
	});

	it('offers no power verbs on a node or a device', async () => {
		listInventory.mockResolvedValue({
			items: [guestRow({ id: 'h-node', hostname: 'pve1', host_type: 'node', console_url: null })],
			total: 1,
		});
		render(Hosts);
		await screen.findByText('pve1', { selector: 'a' });

		await fireEvent.click(screen.getByRole('button', { name: 'Actions for pve1' }));
		const menu = screen.getByRole('menu', { name: 'Actions for pve1' });
		expect(within(menu).queryByRole('menuitem', { name: /^Start/ })).toBeNull();
		expect(within(menu).queryByRole('menuitem', { name: /Console/ })).toBeNull();
		expect(getGuestPower).not.toHaveBeenCalled();
	});
});

describe('Hosts: + Add and the phone card list (UI slice 5)', () => {
	afterEach(() => {
		cleanup();
		// jsdom has no matchMedia of its own; leave none behind.
		delete (window as { matchMedia?: unknown }).matchMedia;
	});
	beforeEach(() => {
		vi.clearAllMocks();
		sessionStore.set({ authenticated: true, capabilities: ['read', 'write', 'admin'] } as never);
		listInventory.mockResolvedValue({ items: [guestRow()], total: 1 });
		getGuestPower.mockResolvedValue(power());
	});

	it('`+ Add` opens the three doors; the missing provision form says so', async () => {
		render(Hosts);
		await screen.findByText('web-01');

		await fireEvent.click(screen.getByRole('button', { name: 'Add to the fleet' }));
		const menu = screen.getByRole('menu', { name: 'Add to the fleet' });

		const provision = within(menu).getByRole('menuitem', { name: /^Provision a VM from template/ });
		expect(provision).toBeDisabled();
		expect(provision.textContent).toContain('Not built yet');
		expect(within(menu).getByRole('menuitem', { name: 'Enrol an agent' }).getAttribute('href')).toBe(
			'/ui/hosts/agents',
		);
		await fireEvent.click(within(menu).getByRole('menuitem', { name: 'Add a LAN device' }));
		expect(screen.getByPlaceholderText('nas01')).toBeTruthy();
	});

	it('on a phone the fleet is a card list with the same row menu', async () => {
		window.matchMedia = vi.fn().mockImplementation((q: string) => ({
			matches: q.includes('max-width'),
			media: q,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		})) as unknown as typeof window.matchMedia;
		render(Hosts);
		await screen.findByText('web-01');

		const cards = await screen.findByTestId('host-cards');
		expect(screen.queryByRole('table')).toBeNull();
		const card = within(cards).getByText('web-01').closest('li') as HTMLElement;
		expect(within(card).getByText('stopped')).toBeTruthy();
		expect(within(card).getByText('VMID 101')).toBeTruthy();
		await fireEvent.click(within(card).getByRole('button', { name: 'Actions for web-01' }));
		const menu = within(card).getByRole('menu', { name: 'Actions for web-01' });
		await waitFor(() => expect(within(menu).getByRole('menuitem', { name: /^Start/ })).toBeEnabled());
	});
});
