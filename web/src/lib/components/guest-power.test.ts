import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '../test-mocks';
import GuestPower from './GuestPower.svelte';
import { ApiError, api, type GuestPowerState } from '$lib/api';

/**
 * Lane B3 (#781): what an operator SEES when powering a guest from its host page.
 *
 * - a shutdown the guest ignored reads "Timed out" and offers Stop, never "Done";
 * - the machine HomePilot runs on cannot be shut down until its name is typed,
 *   and the typed name is what is sent;
 * - Stop (forced) on an ordinary guest asks twice before anything is sent;
 * - a refusal shows the server's reason (the address holder named);
 * - Reboot lists the pending changes it will apply.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - render every outcome as "Done" and the timed-out case fails;
 *   - drop `nameOk` from the confirm button's `disabled` and the self case fails;
 *   - drop the `stopArmed` first ask and the stop case fails.
 */
const getGuestPower = api.getGuestPower as ReturnType<typeof vi.fn>;
const powerGuest = api.powerGuest as ReturnType<typeof vi.fn>;

function guest(over: Partial<GuestPowerState> = {}): GuestPowerState {
	return {
		host_id: 'h1',
		hostname: 'web-01',
		vmid: 101,
		node: 'pve1',
		guest_type: 'qemu',
		is_self: false,
		protected: false,
		protected_reason: null,
		status: 'running',
		pending: [],
		actions: {
			start: { available: false, reason: 'the guest is running, not stopped', needs_typed_confirmation: false },
			shutdown: { available: true, reason: null, needs_typed_confirmation: false },
			reboot: { available: true, reason: null, needs_typed_confirmation: false },
			stop: { available: true, reason: null, needs_typed_confirmation: false },
		},
		error: null,
		...over,
	};
}

describe('GuestPower (lane B3)', () => {
	afterEach(() => cleanup());
	beforeEach(() => vi.clearAllMocks());

	it('reports a shutdown the guest ignored as timed out and offers Stop', async () => {
		getGuestPower.mockResolvedValue(guest());
		powerGuest.mockResolvedValue({
			action: 'shutdown',
			outcome: 'timed_out',
			message: 'web-01 did not shut down within 120s. It is still running; nothing was forced.',
			upid: 'UPID:pve1:1',
			status_after: 'running',
			next_actions: ['stop'],
			pending_after: null,
		});
		render(GuestPower, { hostId: 'h1' });

		await fireEvent.click(await screen.findByRole('button', { name: 'Shut down' }));
		const confirm = screen.getAllByRole('button', { name: 'Shut down' });
		await fireEvent.click(confirm[confirm.length - 1]);

		const status = await screen.findByRole('status');
		expect(status).toHaveTextContent('Timed out:');
		expect(status).not.toHaveTextContent('Done');
		expect(status.getAttribute('data-power-result')).toBe('timed_out');
		expect(powerGuest).toHaveBeenCalledWith('h1', { action: 'shutdown' });
		// The way forward is offered, not taken.
		expect(screen.getAllByRole('button', { name: 'Stop (forced)' }).length).toBeGreaterThan(0);
		expect(powerGuest).toHaveBeenCalledTimes(1);
	});

	it('will not shut down the self guest until its name is typed, and sends the name', async () => {
		const self = guest({ hostname: 'hp-control', is_self: true, protected: true, protected_reason: 'self' });
		for (const a of ['shutdown', 'reboot', 'stop'] as const) {
			self.actions[a] = { available: true, reason: null, needs_typed_confirmation: true };
		}
		getGuestPower.mockResolvedValue(self);
		powerGuest.mockResolvedValue({
			action: 'shutdown', outcome: 'ok', message: 'hp-control shut down.', upid: null,
			status_after: 'stopped', next_actions: [], pending_after: null,
		});
		render(GuestPower, { hostId: 'h1' });

		await fireEvent.click(await screen.findByRole('button', { name: 'Shut down' }));
		expect(screen.getByText(/HomePilot goes down with it/)).toBeInTheDocument();
		const buttons = screen.getAllByRole('button', { name: 'Shut down' });
		const go = buttons[buttons.length - 1];
		expect(go).toBeDisabled();
		const input = screen.getByLabelText("Type the guest's name");
		await fireEvent.input(input, { target: { value: 'wrong' } });
		expect(go).toBeDisabled();
		await fireEvent.input(input, { target: { value: 'hp-control' } });
		expect(go).not.toBeDisabled();
		await fireEvent.click(go);

		await waitFor(() =>
			expect(powerGuest).toHaveBeenCalledWith('h1', { action: 'shutdown', confirm_name: 'hp-control' }),
		);
	});

	it('asks twice before a forced stop of an ordinary guest', async () => {
		getGuestPower.mockResolvedValue(guest());
		powerGuest.mockResolvedValue({
			action: 'stop', outcome: 'ok', message: 'web-01 was stopped (forced).', upid: null,
			status_after: 'stopped', next_actions: [], pending_after: null,
		});
		render(GuestPower, { hostId: 'h1' });

		await fireEvent.click(await screen.findByRole('button', { name: 'Stop (forced)' }));
		const buttons = screen.getAllByRole('button', { name: 'Stop (forced)' });
		await fireEvent.click(buttons[buttons.length - 1]);
		expect(powerGuest).not.toHaveBeenCalled();
		expect(screen.getByText(/Really force it off/)).toBeInTheDocument();

		await fireEvent.click(screen.getByRole('button', { name: 'Yes, force it off' }));
		await waitFor(() => expect(powerGuest).toHaveBeenCalledWith('h1', { action: 'stop' }));
	});

	it("shows the server's refusal, naming the address holder", async () => {
		getGuestPower.mockResolvedValue(
			guest({
				status: 'stopped',
				actions: { start: { available: true, reason: null, needs_typed_confirmation: false } },
			}),
		);
		powerGuest.mockRejectedValue(
			new ApiError(
				409,
				JSON.stringify({
					detail: {
						code: 'address_clash',
						message: 'cannot start web-01: 192.0.2.10 is held by vpn-box (VMID 101, running).',
					},
				}),
			),
		);
		render(GuestPower, { hostId: 'h1' });

		await fireEvent.click(await screen.findByRole('button', { name: 'Start' }));
		const buttons = screen.getAllByRole('button', { name: 'Start' });
		await fireEvent.click(buttons[buttons.length - 1]);

		const alert = await screen.findByRole('alert');
		expect(alert).toHaveTextContent('192.0.2.10 is held by vpn-box');
		expect(screen.queryByRole('status')).toBeNull();
	});

	it('lists the pending changes a reboot will apply', async () => {
		getGuestPower.mockResolvedValue(
			guest({
				pending: [{ key: 'ipconfig0', current: 'ip=192.0.2.10/24', pending: 'ip=192.0.2.20/24' }],
			}),
		);
		render(GuestPower, { hostId: 'h1' });

		const line = await screen.findByText('ipconfig0: ip=192.0.2.10/24 ip=192.0.2.20/24');
		expect(line.tagName).toBe('LI');
		// The arrow between old and new is an icon, and a named one: a screen
		// reader hears "becomes", not two values in a row.
		expect(within(line).getByRole('img', { name: 'becomes' })).toHaveAttribute('data-icon', 'arrow-right');
	});

	it('never shows a state it could not read', async () => {
		getGuestPower.mockRejectedValue(new Error('connection refused'));
		render(GuestPower, { hostId: 'h1' });
		expect(await screen.findByText(/Could not read the power state/)).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Start' })).toBeNull();
	});
});
