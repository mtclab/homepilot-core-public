import { describe, expect, it } from 'vitest';
import { ApiError, type GuestPowerState } from './api';
import {
	isPowerable,
	needsTypedName,
	outcomeLine,
	refusalLine,
	verbStates,
	workingLine,
} from './powerMenu';

/**
 * What the Hosts row menu tells the operator about power (UI slice 5).
 *
 * - every verb is listed; only the ones the LIVE state allows are enabled, and
 *   a disabled one always says why (never just greyed out);
 * - an address-clash refusal names the address AND the holder in one line;
 * - a forced stop, and any verb on the self guest or a protected guest, takes
 *   the typed dialog; an ordinary start/shutdown/reboot does not;
 * - the running line is honest: "following Proxmox task", never "done".
 *
 * Teeth (each verified by reverting): drop the holder branch in refusalLine,
 * or return false for 'stop' in needsTypedName, or emit why='' for a
 * disabled verb - the matching test below fails.
 */
function state(over: Partial<GuestPowerState> = {}): GuestPowerState {
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
		pve_name: 'web-01',
		name_matches: true,
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

const ADMIN = { canAdmin: true, reading: false, unread: '' };

describe('verbStates', () => {
	it('enables what the live state allows and says why for the rest', () => {
		const v = verbStates(state(), ADMIN);
		expect(v.map((x) => [x.action, x.enabled])).toEqual([
			['start', false],
			['shutdown', true],
			['reboot', true],
			['stop', true],
		]);
		expect(v[0].why).toBe('Already running');
		for (const x of v.filter((y) => !y.enabled)) expect(x.why).not.toBe('');
	});

	it('a stopped guest offers only Start, the rest "Not running"', () => {
		const v = verbStates(
			state({
				status: 'stopped',
				actions: {
					start: { available: true, reason: null, needs_typed_confirmation: false },
					shutdown: { available: false, reason: 'x', needs_typed_confirmation: false },
					reboot: { available: false, reason: 'x', needs_typed_confirmation: false },
					stop: { available: false, reason: 'x', needs_typed_confirmation: false },
				},
			}),
			ADMIN,
		);
		expect(v.filter((x) => x.enabled).map((x) => x.action)).toEqual(['start']);
		expect(v.slice(1).every((x) => x.why === 'Not running')).toBe(true);
	});

	it('nothing is offered while reading, after a failed read, or to a non-admin', () => {
		const reading = verbStates(null, { ...ADMIN, reading: true });
		expect(reading.every((x) => !x.enabled && x.why === 'Reading its state from Proxmox')).toBe(true);
		const failed = verbStates(null, { ...ADMIN, unread: 'boom' });
		expect(failed.every((x) => !x.enabled && x.why === 'Could not read its state from Proxmox')).toBe(
			true,
		);
		const reader = verbStates(state(), { ...ADMIN, canAdmin: false });
		expect(reader.every((x) => !x.enabled && x.why === 'Needs an admin session')).toBe(true);
	});

	it('a reused VMID disables everything and names the occupant', () => {
		const v = verbStates(state({ name_matches: false, pve_name: 'someone-else' }), ADMIN);
		expect(v.every((x) => !x.enabled)).toBe(true);
		expect(v[0].why).toBe('VMID 101 is now someone-else; sync first');
	});
});

describe('needsTypedName', () => {
	it('a forced stop is typed on any guest; ordinary verbs are not', () => {
		expect(needsTypedName(state(), 'stop')).toBe(true);
		expect(needsTypedName(state(), 'shutdown')).toBe(false);
		expect(needsTypedName(state(), 'reboot')).toBe(false);
		expect(needsTypedName(state(), 'start')).toBe(false);
	});

	it('every verb on the self guest or a protected guest is typed', () => {
		for (const s of [state({ is_self: true }), state({ protected: true })]) {
			for (const a of ['start', 'shutdown', 'reboot', 'stop'] as const) {
				expect(needsTypedName(s, a)).toBe(true);
			}
		}
	});
});

describe('the lines the row shows', () => {
	it('while running it says it is following the task', () => {
		expect(workingLine('start')).toBe('Starting... following Proxmox task');
		expect(workingLine('shutdown')).toBe('Shutting down... following Proxmox task');
	});

	it('names the address and the holder of a clash', () => {
		const err = new ApiError(
			409,
			JSON.stringify({
				detail: {
					code: 'address_clash',
					message: 'cannot start copycat: 192.0.2.10 is held by relay (VMID 112, running).',
					holders: [{ address: '192.0.2.10', hostname: 'relay', vmid: 112, node: 'pve1' }],
				},
			}),
		);
		expect(refusalLine('start', err)).toBe(
			'Start refused: Address 192.0.2.10 is used by relay (VMID 112). Nothing was sent.',
		);
	});

	it('names the disk and the running guest that holds it', () => {
		const err = new ApiError(
			409,
			JSON.stringify({
				detail: {
					code: 'disk_shared',
					message: 'cannot start old-nas: ...',
					disk: '/dev/disk/by-id/ata-EXAMPLE',
					holder: { hostname: 'nas', vmid: 101, key: 'scsi1' },
				},
			}),
		);
		expect(refusalLine('start', err)).toBe(
			'Start refused: disk /dev/disk/by-id/ata-EXAMPLE is attached to running nas (VMID 101). Nothing was sent.',
		);
	});

	it("any other refusal is the server's own sentence", () => {
		const err = new ApiError(
			409,
			JSON.stringify({ detail: { code: 'locked', message: 'cannot stop web-01: Proxmox has it locked (backup)' } }),
		);
		expect(refusalLine('stop', err)).toBe('Stop refused: cannot stop web-01: Proxmox has it locked (backup)');
		expect(refusalLine('stop', new Error('network down'))).toBe('Stop refused: network down');
	});

	it('a settled outcome is never shown as done unless it is', () => {
		const base = { action: 'shutdown' as const, upid: null, status_after: 'running', next_actions: [], pending_after: null };
		expect(outcomeLine({ ...base, outcome: 'ok', message: 'web-01 shut down.' })).toEqual({ tone: 'ok', text: 'web-01 shut down.' });
		expect(outcomeLine({ ...base, outcome: 'timed_out', message: 'ignored it' }).tone).toBe('warn');
		expect(outcomeLine({ ...base, outcome: 'unknown', message: 'still running' }).text).toMatch(/^Outcome not known yet/);
		expect(outcomeLine({ ...base, outcome: 'failed', message: 'QEMU exited' })).toEqual({ tone: 'crit', text: 'Failed: QEMU exited' });
	});
});

describe('isPowerable', () => {
	it('only a VM or container that exists and is not a template', () => {
		expect(isPowerable({ id: '1', hostname: 'a', host_type: 'qemu' })).toBe(true);
		expect(isPowerable({ id: '1', hostname: 'a', host_type: 'lxc' })).toBe(true);
		expect(isPowerable({ id: '1', hostname: 'a', host_type: 'node' })).toBe(false);
		expect(isPowerable({ id: '1', hostname: 'a', host_type: 'qemu', is_template: 1 })).toBe(false);
		expect(isPowerable({ id: '1', hostname: 'a', host_type: 'qemu', absent_since: '2026-11-01' })).toBe(false);
	});
});
