import { describe, it, expect } from 'vitest';
import { SETTINGS_TABS, SETTINGS_MOVED, settingsTabFromHash, movedSettingsTarget } from './settingsTabs';
import { activeQueryTab, queryTabs } from './tabs';

/**
 * Settings' sectors are ADDRESSES (#549 F6, principle 2).
 *
 * Teeth: drop a tab from SETTINGS_TABS and the list test fails; make the tabs
 * component state instead of `?tab=` and the addressability test fails; delete
 * a legacy fragment from the map and the deep-link case it names fails on the
 * Connection fallback instead of its own tab.
 */
describe('Settings tab list', () => {
	// UI slice 1: Settings is plumbing only. Monitoring, Guests and Tokens
	// left for pages of their own (SETTINGS_MOVED, asserted below).
	it('is exactly the four plumbing sectors, in order', () => {
		expect(SETTINGS_TABS.map((t) => t.id)).toEqual(['connection', 'proxmox', 'subsystems', 'about']);
	});

	it('addresses every sector with ?tab= on the settings route', () => {
		const tabs = queryTabs(SETTINGS_TABS, '/ui/settings');
		expect(tabs.map((t) => t.href)).toEqual([
			'/ui/settings?tab=connection',
			'/ui/settings?tab=proxmox',
			'/ui/settings?tab=subsystems',
			'/ui/settings?tab=about',
		]);
	});

	it('selects the tab the URL names, and falls back to Connection', () => {
		const at = (q: string) => activeQueryTab(SETTINGS_TABS, new URL('http://x/ui/settings' + q));
		expect(at('?tab=about')).toBe('about');
		expect(at('?tab=subsystems')).toBe('subsystems');
		// No tab, and a stale bookmark naming a sector that no longer exists:
		// both land on a real sector rather than on nothing.
		expect(at('')).toBe('connection');
		expect(at('?tab=nonesuch')).toBe('connection');
	});
});

describe('Legacy links into a Settings section', () => {
	// Fragment on the left, the tab that swallowed that card on the right.
	const CASES: Array<[string, string]> = [
		['#tokens', 'tokens'],
		['#proxmox', 'proxmox'],
		['#proxmox-connection', 'proxmox'],
		['#health', 'connection'],
		['#system-health', 'connection'],
		['#api-connection', 'connection'],
		['#selfcheck', 'subsystems'],
		['#optional-subsystems', 'subsystems'],
		['#guests', 'guests'],
		['#alerts', 'monitoring'],
		['#about', 'about'],
		// Case is the copier's, not the author's.
		['#Tokens', 'tokens'],
	];

	for (const [hash, tab] of CASES) {
		it(`${hash} lands on ${tab}`, () => {
			expect(settingsTabFromHash(hash)).toBe(tab);
		});
	}

	it('leaves a fragment Settings never had alone', () => {
		// '' means "no opinion", so the page does not redirect and normal ?tab=
		// resolution answers. Returning 'connection' here would rewrite the URL
		// of every anchor-carrying link in the world.
		expect(settingsTabFromHash('#somewhere-else')).toBe('');
		expect(settingsTabFromHash('')).toBe('');
	});

	it('names a tab that exists, or a sector that moved to a page of its own', () => {
		const ids = new Set(SETTINGS_TABS.map((t) => t.id));
		for (const [hash] of CASES) {
			const tab = settingsTabFromHash(hash);
			expect(ids.has(tab) || tab in SETTINGS_MOVED, `${hash} -> ${tab}`).toBe(true);
		}
	});
});

/**
 * UI slice 1: the sectors that left Settings. Teeth: drop an entry from
 * SETTINGS_MOVED and its case lands nowhere (null); stop carrying the query
 * or the fragment and the intent cases fail.
 */
describe('A Settings URL that names a moved sector', () => {
	const at = (u: string) => movedSettingsTarget(new URL('http://x/ui/settings' + u));

	it('moves exactly Monitoring, Guests and Tokens', () => {
		expect(SETTINGS_MOVED).toEqual({
			monitoring: '/monitoring/rules',
			guests: '/guests',
			tokens: '/assistants',
		});
		// None of them is still a Settings tab: moved, not duplicated.
		const ids = new Set(SETTINGS_TABS.map((t) => t.id));
		for (const k of Object.keys(SETTINGS_MOVED)) expect(ids.has(k)).toBe(false);
	});

	it('leaves a sector Settings still has alone', () => {
		expect(at('')).toBeNull();
		expect(at('?tab=proxmox')).toBeNull();
		expect(at('#health')).toBeNull();
		expect(at('#somewhere-else')).toBeNull();
	});

	it('carries the rest of the query and an unrelated fragment', () => {
		expect(at('?tab=monitoring&rule=r1')).toBe('/monitoring/rules?rule=r1');
		expect(at('?tab=guests#invites')).toBe('/guests#invites');
	});

	it('lets an explicit ?tab= win over a fragment', () => {
		expect(at('?tab=about#tokens')).toBeNull();
		// A fragment that named a Settings card means nothing on the new page.
		expect(at('?tab=tokens#about')).toBe('/assistants');
	});
});
