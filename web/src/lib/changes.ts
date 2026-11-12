// The Changes inbox in words (owner feedback 2026-11-08, UI_PLAN 3.6).
//
// The owner approved a change, it left the review queue, and nothing said it
// still had to be applied: an empty queue read as "lost". So a change keeps ONE
// place - the Inbox - from proposal until it has run, and every state it can be
// in says, in plain words, what happens next. These helpers are pure so the
// words are tested without a page around them.

import type { Artifact, ArtifactTarget, LastTask, LiveGuest, PveWarning } from './api';
/** Lamp.svelte's states, repeated here so this module stays plain TS. */
type LampState = 'ok' | 'warn' | 'crit' | 'accent' | 'off' | 'half';

export type InboxState =
	| 'review'
	| 'ready'
	| 'running'
	| 'applied'
	| 'partial'
	| 'failed'
	| 'withdrawn'
	| 'other';

export const STATE_LABEL: Record<InboxState, string> = {
	review: 'To review',
	ready: 'Ready to run',
	running: 'Running',
	applied: 'Applied',
	partial: 'Partly applied',
	failed: 'Failed',
	withdrawn: 'Withdrawn',
	other: '',
};

export const STATE_LAMP: Record<InboxState, LampState> = {
	review: 'accent',
	ready: 'warn',
	running: 'half',
	applied: 'ok',
	partial: 'crit',
	failed: 'crit',
	withdrawn: 'off',
	other: 'off',
};

/** Where a change stands, as the Inbox shows it. A run in flight wins. */
export function inboxState(
	fm: Pick<Artifact, 'status' | 'partial_apply'>,
	activeTask: { status: string } | null | undefined,
): InboxState {
	if (activeTask) return 'running';
	switch (fm.status) {
		case 'proposed':
			return 'review';
		case 'approved':
			return 'ready';
		case 'applied':
			return 'applied';
		case 'failed':
			return fm.partial_apply ? 'partial' : 'failed';
		case 'withdrawn':
			return 'withdrawn';
		default:
			return 'other';
	}
}

/** `18:28` in the viewer's time zone; '' for a missing or unreadable stamp. */
export function hhmm(iso: string | null | undefined): string {
	if (!iso) return '';
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return '';
	return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** "Running: Back up VM 104, started 18:28" (UI_PLAN owner feedback). */
export function runningLine(intent: string, startedAt?: string | null): string {
	const at = hhmm(startedAt);
	return at ? `Running: ${intent}, started ${at}` : `Running: ${intent}`;
}

function capital(s: string): string {
	return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/**
 * How it ended, in words, from the artifact and its last task. Never a bare
 * status: a partial apply names what did happen (#796), a withdrawal says it
 * never ran, an unknown outcome says it is unknown.
 */
export function outcomeSentence(fm: Artifact, last: LastTask | null | undefined): string {
	const at = hhmm(last?.finished_at ?? null);
	const when = at ? ` at ${at}` : '';
	if (last && last.status === 'unknown') {
		return `Outcome unknown: ${last.error ?? 'the Proxmox task was still running when the run stopped following it'}. Open the change to ask Proxmox again.`;
	}
	switch (fm.status) {
		case 'applied':
			return `Done${when}: ${fm.intent}.`;
		case 'failed':
			if (fm.partial_apply) return `${capital(fm.failure_reason ?? 'partly applied')}.`;
			return `Failed${when}: ${fm.failure_reason ?? last?.error ?? 'no reason recorded'}.`;
		case 'withdrawn': {
			const by = fm.withdrawn_by?.user ? ` by ${fm.withdrawn_by.user}` : '';
			const why = fm.withdrawn_by?.reason ? ` (${fm.withdrawn_by.reason})` : '';
			return `Withdrawn${by}${why}. It was never run.`;
		}
		default:
			return '';
	}
}

/** The warnings of the last run, verbatim, with their verdict. */
export function warningsOf(last: LastTask | null | undefined): PveWarning[] {
	return last?.result?.warnings ?? [];
}

/** The short verdict beside a warning. */
export function warningVerdict(w: PveWarning): string {
	if (w.harmless === true) return 'harmless';
	if (w.harmless === false) return 'needs attention';
	return 'not recognised';
}

/** Statuses the Inbox holds: everything still waiting on someone. */
export const INBOX_STATUSES = ['proposed', 'approved'] as const;

/** How long a finished change stays in the Inbox's "Finished" list. */
export const RECENT_MS = 24 * 3600 * 1000;

/** Finished in the last day, newest first - so a change just run is not gone. */
export function recentlyFinished(items: Artifact[], now: number = Date.now()): Artifact[] {
	const when = (a: Artifact): number => {
		const raw = a.applied_at ?? a.failed_at ?? a.withdrawn_by?.at ?? '';
		const t = raw ? new Date(raw).getTime() : NaN;
		return Number.isNaN(t) ? 0 : t;
	};
	return items
		.filter((a) => now - when(a) <= RECENT_MS && when(a) > 0)
		.sort((a, b) => when(b) - when(a));
}

/** Whole days since an ISO stamp; null for a missing or unreadable one. */
export function daysSince(iso: string | null | undefined, now: number = Date.now()): number | null {
	if (!iso) return null;
	const t = new Date(iso).getTime();
	if (Number.isNaN(t)) return null;
	return Math.max(0, Math.floor((now - t) / (24 * 3600 * 1000)));
}

/** "approved today" / "approved 1 day ago" / "approved 74 days ago" (#813). */
export function approvalAge(iso: string | null | undefined, now: number = Date.now()): string {
	const days = daysSince(iso, now);
	if (days === null) return 'approval time unknown';
	if (days === 0) return 'approved today';
	return `approved ${days} day${days === 1 ? '' : 's'} ago`;
}

function gib(bytes: number | undefined): string | null {
	if (!bytes || bytes <= 0) return null;
	const g = bytes / 1024 ** 3;
	return `${g >= 10 ? Math.round(g) : Math.round(g * 10) / 10} GB`;
}

/** "running · 2 cores · 8 GB RAM · 30 GB disk · owner alice": the guest as it is now. */
export function liveStateLine(g: LiveGuest): string {
	const parts: string[] = [g.status ?? 'state unknown'];
	if (g.cpus) parts.push(`${g.cpus} core${g.cpus === 1 ? '' : 's'}`);
	const mem = gib(g.maxmem);
	if (mem) parts.push(`${mem} RAM`);
	const disk = gib(g.maxdisk);
	if (disk) parts.push(`${disk} disk`);
	if (g.owner) parts.push(`owner ${g.owner}`);
	return parts.join(' · ');
}

/** Why the server's run guard will not run this change, or null when it will. */
export function cannotRun(t: ArtifactTarget | null | undefined): string | null {
	if (!t || t.may_run) return null;
	return t.approval.stale ?? t.identity.message ?? 'its target could not be confirmed';
}

/**
 * What the confirm dialog of a destroy or forced stop offers (#813), from a
 * live read of the target. The typed name is the guest's name AS READ NOW -
 * never its VMID, which a different machine at the same number shares - and
 * a change whose machine is not the recorded one is not offered at all.
 */
export interface LiveConfirm {
	/** Why nothing is offered, or null. */
	blocked: string | null;
	/** The name to type back: the guest's name as read now. */
	name: string | null;
	vmid: number | null;
	/** The guest as it is now, beside the name. */
	state: string;
}

export function liveConfirm(t: ArtifactTarget | null): LiveConfirm {
	const none = { name: null, vmid: null, state: '' };
	if (!t) return { ...none, blocked: 'Could not ask who is at the VMID now; nothing can be confirmed.' };
	const why = cannotRun(t);
	if (why) return { ...none, blocked: `This change will not run: ${why}.` };
	if (!t.confirm?.name) {
		return { ...none, blocked: 'The guest this change acts on could not be read, so nothing can be confirmed.' };
	}
	return { blocked: null, name: t.confirm.name, vmid: t.confirm.vmid, state: liveStateLine(t.confirm) };
}
