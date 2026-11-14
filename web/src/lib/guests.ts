// The Guests page's words and numbers (3.12), kept pure so they are asserted
// without rendering: budget bars, the "used up" sentence, the budget check the
// Invite dialog says before minting, and an invite's history in words.
//
// The budget check mirrors `check_provision` (src/homepilot/guest/quota.py)
// axis for axis. It is advisory - the server decides at redemption - and it is
// here so the operator learns "this would be refused" BEFORE sending a link,
// not from their friend.
import type { Friend, FriendInvite, GuestLimits, InviteSize } from '$lib/api';
import { inWords } from '$lib/relativeTime';

export type Axis = keyof GuestLimits;
export const AXES: Axis[] = ['vms', 'cores', 'memory_mb', 'disk_gb'];
export const AXIS_WORD: Record<Axis, string> = {
	vms: 'machines',
	cores: 'cores',
	memory_mb: 'memory',
	disk_gb: 'disk',
};

/** Megabytes as gigabytes for reading: 1024 -> "1", 1536 -> "1.5", 300 -> "0.29". */
export function gb(mb: number | null | undefined): string {
	if (mb == null) return '?';
	const v = mb / 1024;
	return String(Number(v.toFixed(v < 1 ? 2 : 1)));
}

/** "1 core · 1 GB · 30 GB" - one machine's size. */
export function sizeLine(s: { cores: number | null; memory_mb: number | null; disk_gb: number | null }): string {
	const cores = s.cores == null ? '? cores' : `${s.cores} ${s.cores === 1 ? 'core' : 'cores'}`;
	return `${cores} · ${gb(s.memory_mb)} GB · ${s.disk_gb ?? '?'} GB`;
}

/** One axis as the bar reads it: "1 of 1", "1 of 1 GB", "30 of 30 GB". */
function amount(axis: Axis, n: number): string {
	if (axis === 'memory_mb') return gb(n);
	return String(n);
}
function unit(axis: Axis): string {
	return axis === 'memory_mb' || axis === 'disk_gb' ? ' GB' : '';
}

export interface BudgetBar {
	axis: Axis;
	word: string;
	text: string;
	/** 0-100, for the bar's width. */
	pct: number;
	full: boolean;
}

/** Bars for the axes that have a limit, and the axes that do not. */
export function budgetBars(f: Friend): { bars: BudgetBar[]; unlimited: Axis[] } {
	const bars: BudgetBar[] = [];
	const unlimited: Axis[] = [];
	for (const axis of AXES) {
		const limit = f.limits?.[axis];
		const used = f.usage[axis];
		if (limit == null) {
			unlimited.push(axis);
			continue;
		}
		const pct = limit <= 0 ? 100 : Math.min(100, Math.round((used / limit) * 100));
		bars.push({
			axis,
			word: AXIS_WORD[axis],
			text: `${amount(axis, used)} of ${amount(axis, limit)}${unit(axis)}`,
			pct,
			full: used >= limit,
		});
	}
	return { bars, unlimited };
}

/** The axes on which the friend has nothing left (used >= limit). */
export function usedUp(f: Friend): Axis[] {
	return AXES.filter((a) => f.limits?.[a] != null && f.usage[a] >= (f.limits?.[a] as number));
}

/** The plain sentence when the budget is used up, or null when it is not. */
export function usedUpSentence(f: Friend): string | null {
	const full = usedUp(f);
	if (!full.length) return null;
	const which = full.map((a) => AXIS_WORD[a]).join(', ');
	return `The budget is used up (${which}): ${f.cn} cannot build another machine until you raise it.`;
}

/**
 * Which axes would refuse a machine of this size at redemption - the same
 * arithmetic as `check_provision`: one more machine, plus its cores, memory and
 * disk, against each limit that is set.
 */
export function wouldRefuse(f: Friend | null | undefined, size: InviteSize): Axis[] {
	if (!f?.limits) return [];
	const l = f.limits;
	const u = f.usage;
	const out: Axis[] = [];
	if (l.vms != null && u.vms + 1 > l.vms) out.push('vms');
	if (l.cores != null && u.cores + size.cores > l.cores) out.push('cores');
	if (l.memory_mb != null && u.memory_mb + size.memory_mb > l.memory_mb) out.push('memory_mb');
	if (l.disk_gb != null && u.disk_gb + size.disk_gb > l.disk_gb) out.push('disk_gb');
	return out;
}

/** The one line the Invite dialog says about the budget for this size. */
export function budgetCheck(
	f: Friend | null | undefined,
	cn: string,
	size: InviteSize,
): { fits: boolean; text: string } {
	const name = cn.trim();
	if (!name) return { fits: true, text: '' };
	if (!f?.limits) return { fits: true, text: `No budget for ${name}: nothing caps what they hold.` };
	const refused = wouldRefuse(f, size);
	if (refused.length) {
		const lines = refused.map((a) => {
			const limit = f.limits?.[a] as number;
			return `${AXIS_WORD[a]} ${amount(a, f.usage[a])} of ${amount(a, limit)}${unit(a)} used`;
		});
		return {
			fits: false,
			text: `${name}'s budget would refuse this when they open the link (${lines.join(', ')}). Raise the budget first.`,
		};
	}
	return { fits: true, text: `Fits ${name}'s budget.` };
}

/** What an invite is, in words, and the moment the words are about. */
export function inviteWords(inv: FriendInvite, now: Date = new Date()): { text: string; at: string | null } {
	switch (inv.state) {
		case 'redeemed':
			return { text: `redeemed ${inWords(inv.redeemed_at, now)}`, at: inv.redeemed_at };
		case 'revoked':
			return { text: `revoked ${inWords(inv.revoked_at, now)}`, at: inv.revoked_at };
		case 'expired':
			return { text: `expired ${inWords(inv.expires_at, now)}`, at: inv.expires_at };
		case 'open':
			return { text: `open, expires ${inWords(inv.expires_at, now)}`, at: inv.expires_at };
		default:
			return { text: inv.state, at: null };
	}
}

/** "2 earlier redeemed, 1 expired" - the invites a folded history does not show. */
export function earlierSummary(rest: FriendInvite[]): string {
	if (!rest.length) return '';
	const counts = new Map<string, number>();
	for (const inv of rest) counts.set(inv.state, (counts.get(inv.state) ?? 0) + 1);
	const order = ['open', 'redeemed', 'expired', 'revoked'];
	const states = [...counts.keys()].sort((a, b) => order.indexOf(a) - order.indexOf(b));
	return states
		.map((s, i) => `${counts.get(s)} ${i === 0 ? 'earlier ' : ''}${s}`)
		.join(', ');
}

/**
 * The invites a folded history shows: the latest, plus every other OPEN one -
 * an open invite is something the operator can act on (Revoke), so it is never
 * folded away.
 */
export function foldedInvites(invites: FriendInvite[]): { shown: FriendInvite[]; rest: FriendInvite[] } {
	const shown = invites.filter((inv, i) => i === 0 || inv.state === 'open');
	const rest = invites.filter((inv) => !shown.includes(inv));
	return { shown, rest };
}

/** Lamp and tag tone for a machine's state word. */
export function machineTone(state: string): { lamp: 'ok' | 'half' | 'off'; tag: 'ok' | 'muted' } {
	switch (state) {
		case 'running':
		case 'online':
			return { lamp: 'ok', tag: 'ok' };
		case 'stopped':
		case 'offline':
		case 'paused':
			return { lamp: 'half', tag: 'muted' };
		default:
			return { lamp: 'off', tag: 'muted' };
	}
}
