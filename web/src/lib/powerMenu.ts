// The power verbs in the Hosts row menu (UI slice 5, plan 3.2 / 5.3 / 5.4).
//
// Pure functions, so what the operator is told - which verbs are offered,
// why one is not, what a refusal names, what a running action says - is
// asserted without mounting a table. The page only wires them up.
//
// The state the verbs are judged by is the SERVER's live read of Proxmox
// (`GET /inventory/{id}/power`), fetched when the menu opens - never the
// inventory row, which can be a sync interval old.

import type { GuestPowerResult, GuestPowerState, Host, PowerAction } from './api';

export const VERBS: PowerAction[] = ['start', 'shutdown', 'reboot', 'stop'];

/** The same words everywhere (plan 5.2): Start / Shut down / Reboot / Stop. */
export const VERB_LABEL: Record<PowerAction, string> = {
	start: 'Start',
	shutdown: 'Shut down',
	reboot: 'Reboot',
	stop: 'Stop',
};

const VERB_ING: Record<PowerAction, string> = {
	start: 'Starting',
	shutdown: 'Shutting down',
	reboot: 'Rebooting',
	stop: 'Stopping',
};

/** A row the power verbs belong on: a Proxmox VM or container that exists and can run. */
export function isPowerable(h: Host): boolean {
	return (h.host_type === 'qemu' || h.host_type === 'lxc') && !h.is_template && !h.absent_since;
}

export interface VerbState {
	action: PowerAction;
	label: string;
	enabled: boolean;
	/** Why it is not offered, in a few words; '' when it is. */
	why: string;
}

export interface VerbContext {
	canAdmin: boolean;
	/** The live read is in flight. */
	reading: boolean;
	/** The live read failed: its message. */
	unread: string;
}

/** Why the guest's state rules a verb out, or '' when the state allows it. */
function stateWhy(action: PowerAction, status: string): string {
	if (action === 'start') {
		if (status === 'stopped') return '';
		return status === 'running' ? 'Already running' : `It is ${status}`;
	}
	if (status === 'running') return '';
	return status === 'stopped' ? 'Not running' : `It is ${status}`;
}

/**
 * Every verb, enabled only when the server says it applies NOW; a disabled one
 * always carries its reason (plan 3.2: "disabled with reason").
 */
export function verbStates(state: GuestPowerState | null, ctx: VerbContext): VerbState[] {
	const all = (why: string): VerbState[] =>
		VERBS.map((action) => ({ action, label: VERB_LABEL[action], enabled: false, why }));
	if (!ctx.canAdmin) return all('Needs an admin session');
	if (ctx.reading) return all('Reading its state from Proxmox');
	if (ctx.unread) return all('Could not read its state from Proxmox');
	if (!state) return all('Could not read its state from Proxmox');
	if (state.error) return all(state.error);
	if (state.name_matches === false) {
		return all(`VMID ${state.vmid} is now ${state.pve_name ?? 'another machine'}; sync first`);
	}
	return VERBS.map((action) => {
		const a = state.actions[action];
		const enabled = !!a?.available;
		return {
			action,
			label: VERB_LABEL[action],
			enabled,
			why: enabled ? '' : stateWhy(action, state.status) || a?.reason || 'Not available now',
		};
	});
}

/**
 * Typed dialog (plan 5.3 tier 3) or an inline confirm (tier 2)? A forced stop
 * is destructive on any guest; on the machine HomePilot runs on, or a
 * protected guest, every verb is.
 */
export function needsTypedName(state: GuestPowerState, action: PowerAction): boolean {
	return (
		action === 'stop' ||
		state.is_self ||
		state.protected ||
		!!state.actions[action]?.needs_typed_confirmation
	);
}

/** What the row says while the action runs (plan 5.4: honest, not optimistic). */
export function workingLine(action: PowerAction): string {
	return `${VERB_ING[action]}... following Proxmox task`;
}

export type LineTone = 'ok' | 'warn' | 'crit' | 'muted';

/** The settled outcome, as the server judged it after the task ended. */
export function outcomeLine(result: GuestPowerResult): { tone: LineTone; text: string } {
	switch (result.outcome) {
		case 'ok':
			return { tone: 'ok', text: result.message };
		case 'timed_out':
			return { tone: 'warn', text: `Timed out: ${result.message}` };
		case 'unknown':
			return { tone: 'warn', text: `Outcome not known yet: ${result.message}` };
		default:
			return { tone: 'crit', text: `Failed: ${result.message}` };
	}
}

interface Holder {
	address?: string;
	hostname?: string;
	vmid?: number | null;
}

/**
 * A refusal in one line that names the reason AND the holder (plan slice 5:
 * "a Start refused for an address clash names the holder inline"). The
 * server's structured detail is used when it sent one; otherwise its sentence.
 */
export function refusalLine(action: PowerAction, err: unknown): string {
	const info = (err as { info?: Record<string, unknown> | null } | null)?.info ?? null;
	const verb = VERB_LABEL[action];
	if (info && info.code === 'address_clash' && Array.isArray(info.holders) && info.holders.length) {
		const named = (info.holders as Holder[])
			.map(
				(h) =>
					`Address ${h.address} is used by ${h.hostname}` +
					(h.vmid !== null && h.vmid !== undefined ? ` (VMID ${h.vmid})` : ''),
			)
			.join('; ');
		return `${verb} refused: ${named}. Nothing was sent.`;
	}
	if (info && info.code === 'disk_shared' && info.holder && typeof info.disk === 'string') {
		const h = info.holder as Holder;
		return `${verb} refused: disk ${info.disk} is attached to running ${h.hostname} (VMID ${h.vmid}). Nothing was sent.`;
	}
	const message =
		(info && typeof info.message === 'string' && info.message) ||
		(err instanceof Error ? err.message : String(err));
	return `${verb} refused: ${message}`;
}
