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

export const SETTINGS_TABS: QueryTabDef[] = [
	{ id: 'connection', label: 'Connection' },
	{ id: 'proxmox', label: 'Proxmox' },
	{ id: 'subsystems', label: 'Subsystems' },
	{ id: 'guests', label: 'Guests' },
	{ id: 'monitoring', label: 'Monitoring' },
	{ id: 'tokens', label: 'Tokens' },
	{ id: 'about', label: 'About' },
];

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
