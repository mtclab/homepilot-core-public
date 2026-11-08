// Settings' sectors (#549 F6, principle 2) and the legacy links into them.
//
// Settings was one column of seven stacked cards, so an operator who came to
// mint a token scrolled past the API base, the health dump, the self-check and
// the whole Proxmox form to reach it. The cards become `?tab=` tabs on the same
// route - Settings has no id in its path, so the sector is query state.
//
// The addressing lives here, next to the tab list, for the same reason the host
// page's does: the promise that matters is "every link that used to work still
// lands in the right place", and that promise has to be assertable without a
// DOM.

import type { QueryTabDef } from './tabs';

// Settings is plumbing only (UI slice 1, owner decision 1): Monitoring,
// Guests and Tokens were operations and left for pages of their own - see
// SETTINGS_MOVED below for where each one went.
export const SETTINGS_TABS: QueryTabDef[] = [
	{ id: 'connection', label: 'Connection' },
	{ id: 'proxmox', label: 'Proxmox' },
	{ id: 'subsystems', label: 'Subsystems' },
	{ id: 'about', label: 'About' },
];

/**
 * Sectors that LEFT Settings, by the `?tab=` id (or legacy fragment) that
 * used to name them, and the app-relative page that has them now. A bookmark
 * to `/settings?tab=monitoring` must land on the rules it meant, not on the
 * Connection fallback (UI slice 1: every old URL redirects to its new home).
 */
export const SETTINGS_MOVED: Record<string, string> = {
	monitoring: '/monitoring/rules',
	guests: '/guests',
	tokens: '/assistants',
};

/**
 * Links written while Settings was one page name a section by fragment
 * (`/ui/settings#tokens`, and the retired `/ui/tokens` route which pointed at
 * the token card). They keep working: the fragment resolves to the tab that
 * swallowed the section and the page rewrites the URL to the `?tab=` form.
 *
 * The API-base form and the health dump were two cards and are one tab now, so
 * both fragments answer `connection`.
 */
const LEGACY_ANCHORS: Record<string, string> = {
	connection: 'connection',
	api: 'connection',
	'api-connection': 'connection',
	health: 'connection',
	'system-health': 'connection',
	proxmox: 'proxmox',
	'proxmox-connection': 'proxmox',
	subsystems: 'subsystems',
	selfcheck: 'subsystems',
	'optional-subsystems': 'subsystems',
	// Moved out of Settings; resolved through SETTINGS_MOVED.
	guests: 'guests',
	monitoring: 'monitoring',
	alerts: 'monitoring',
	tokens: 'tokens',
	about: 'about',
};

/**
 * The tab a legacy fragment belongs to, or `''` when the fragment names nothing
 * Settings ever had. An unknown fragment must NOT be turned into a redirect:
 * leaving it alone lets the normal `?tab=` resolution (which falls back to
 * Connection) answer, rather than inventing a sector.
 */
export function settingsTabFromHash(hash: string): string {
	const key = hash.replace(/^#/, '').toLowerCase();
	return LEGACY_ANCHORS[key] ?? '';
}

/**
 * Where a Settings URL that names a moved sector belongs now, or `null` when
 * it names a sector Settings still has (or nothing). The explicit `?tab=`
 * wins over a fragment, as it does for the tabs that stayed. The rest of the
 * query (minus `tab`) and a fragment that is not the sector's own name are
 * carried, so intent beyond the sector survives the move.
 */
export function movedSettingsTarget(url: URL): string | null {
	const tab = url.searchParams.get('tab');
	const fromHash = settingsTabFromHash(url.hash);
	const sector = tab ?? (fromHash || null);
	if (!sector || !(sector in SETTINGS_MOVED)) return null;
	const rest = new URLSearchParams(url.search);
	rest.delete('tab');
	const query = rest.toString();
	// A fragment that only named the old sector has done its job; any other
	// fragment is the operator's and travels on.
	const keepHash = url.hash && !fromHash ? url.hash : '';
	return SETTINGS_MOVED[sector] + (query ? `?${query}` : '') + keepHash;
}
