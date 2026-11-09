import { writable } from 'svelte/store';

// Same-origin guard for `returnTo` redirects. An attacker-supplied `returnTo`
// must never send the user off-origin (open redirect) and must never reach
// SvelteKit's `goto` as a full/off-app URL — `goto` throws on those, which made
// a SUCCESSFUL login look like it failed. Returns a safe in-app path, falling
// back when the input is missing, malformed, off-origin, or outside `base`.
export function safeReturnTo(
	raw: string | null | undefined,
	base: string,
	fallback: string,
): string {
	if (!raw) return fallback;

	let value = raw;
	try {
		value = decodeURIComponent(raw);
	} catch {
		return fallback;
	}

	// Must be an absolute in-app path, never a full URL (`http://…`),
	// protocol-relative (`//evil.com`), or backslash-tricked (`/\evil.com`).
	if (!value.startsWith('/')) return fallback;
	if (value.startsWith('//') || value.startsWith('/\\')) return fallback;

	// Must stay within the app's base path.
	if (value !== base && !value.startsWith(base + '/')) return fallback;

	return value;
}

// ---------------------------------------------------------------------------
// The sidebar (UI slice 1, owner decisions 2026-11-08).
//
// Seven nouns in four jobs: the glance (Overview), the estate (Hosts,
// Monitoring, Guests), what changes it (Changes, Assistants), and the record
// and plumbing (Records, Settings). Monitoring is its own noun (decision 1);
// Agents is a tab under Hosts (decision 2), not a noun.
//
// The model is data, not markup, so "every entry reaches a page" and "Guests
// is gated" are assertable without mounting the shell.
// ---------------------------------------------------------------------------

/** Which summary number a nav entry shows. */
export type NavCountKey = 'firing' | 'proposed';

export interface NavItem {
	/** App-relative path WITHOUT the SvelteKit base. */
	href: string;
	label: string;
	count?: NavCountKey;
	/** Shown only when this gate is open. */
	gate?: 'guests';
}

export interface NavGroup {
	/** Sentence-case group label; null for the ungrouped head (Overview). */
	label: string | null;
	items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
	{ label: null, items: [{ href: '/', label: 'Overview' }] },
	{
		label: 'Estate',
		items: [
			{ href: '/hosts', label: 'Hosts' },
			{ href: '/monitoring', label: 'Monitoring', count: 'firing' },
			{ href: '/guests', label: 'Guests', gate: 'guests' },
		],
	},
	{
		label: 'Change',
		items: [
			{ href: '/changes', label: 'Changes', count: 'proposed' },
			{ href: '/assistants', label: 'Assistants' },
		],
	},
	{
		label: 'Record',
		items: [
			{ href: '/records', label: 'Records' },
			{ href: '/settings', label: 'Settings' },
		],
	},
];

export interface NavGates {
	guests: boolean;
}

/** The groups with gated entries removed; a group left empty is dropped. */
export function visibleNav(groups: NavGroup[], gates: NavGates): NavGroup[] {
	return groups
		.map((g) => ({ ...g, items: g.items.filter((i) => !i.gate || gates[i.gate]) }))
		.filter((g) => g.items.length > 0);
}

/**
 * Guests is a noun only where friends' machines exist (UI plan Q8: a fresh
 * install must not see an empty room). The evidence is the guest overview an
 * admin can read: any guest with a budget or machines, or any invite ever
 * minted. A non-admin cannot read it and could not act on the page either.
 * The route itself always renders, and Settings > Subsystems links to it, so
 * the first invite is reachable before the noun appears.
 */
export function guestsGateOpen(
	overview: { guests: unknown[]; invites: unknown[] } | null | undefined,
	isAdmin: boolean,
): boolean {
	if (!isAdmin || !overview) return false;
	return overview.guests.length + overview.invites.length > 0;
}

export interface NavCount {
	n: number;
	tone: 'crit' | 'warn' | 'muted';
	/** Spoken after the label, e.g. "2 firing". */
	label: string;
}

/**
 * The numbers the sidebar shows, from the dashboard summary only - the one
 * read the Overview already makes. A count that is zero is absent (a boxed
 * "0" beside every noun is noise), and an unreadable summary yields NO count
 * rather than a zero: silence must never be mistaken for calm.
 *
 * The firing count is warn-toned: the summary knows how many rules fire, not
 * how severe they are, and painting it critical would claim what it does not
 * know.
 */
export function navCounts(
	summary:
		| {
				metrics?: { firing_alerts?: number };
				artifacts?: Record<string, number>;
				changes?: { proposed?: number; ready?: number; running?: number };
		  }
		| null
		| undefined,
): Partial<Record<NavCountKey, NavCount>> {
	const out: Partial<Record<NavCountKey, NavCount>> = {};
	if (!summary) return out;
	const firing = summary.metrics?.firing_alerts ?? 0;
	if (firing > 0) out.firing = { n: firing, tone: 'warn', label: `${firing} firing` };
	// Everything the Changes inbox holds (owner feedback 2026-11-08): proposals
	// AND approved changes still to run or running. Counting proposals alone made
	// an approved change vanish from the count the moment it was approved, and a
	// queue with work still in it read as empty. An older backend without
	// `changes` falls back to the proposals it does report.
	const proposed = summary.changes?.proposed ?? summary.artifacts?.proposed ?? 0;
	const ready = summary.changes?.ready ?? 0;
	const running = summary.changes?.running ?? 0;
	const n = proposed + ready + running;
	if (n > 0) {
		const parts = [
			proposed > 0 ? `${proposed} proposed` : '',
			ready > 0 ? `${ready} ready to run` : '',
			running > 0 ? `${running} running` : '',
		].filter(Boolean);
		out.proposed = { n, tone: 'muted', label: parts.join(', ') };
	}
	return out;
}

/**
 * The sidebar's counts as the shell last read them, for the views that repeat
 * one (the Changes > Inbox tab label). One read, one source: the tab and the
 * sidebar cannot disagree, and a page does not re-ask the summary.
 */
export const shellCounts = writable<Partial<Record<NavCountKey, NavCount>>>({});

/** One answer to "is this the page I am on", for the highlight and for
 *  `aria-current`. */
export function isCurrentNav(href: string, path: string, base: string): boolean {
	if (href === '/') return path === base + '/' || path === base;
	return path === base + href || path.startsWith(base + href + '/');
}
