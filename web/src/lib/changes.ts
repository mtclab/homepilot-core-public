// The Changes inbox in words (owner feedback 2026-11-08, UI_PLAN 3.6).
//
// The owner approved a change, it left the review queue, and nothing said it
// still had to be applied: an empty queue read as "lost". So a change keeps ONE
// place - the Inbox - from proposal until it has run, and every state it can be
// in says, in plain words, what happens next. These helpers are pure so the
// words are tested without a page around them.

import type { Artifact, LastTask, PveWarning } from './api';
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
