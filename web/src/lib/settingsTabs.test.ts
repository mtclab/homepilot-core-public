import { describe, it, expect } from 'vitest';
import { SETTINGS_TABS, settingsTabFromHash } from './settingsTabs';
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
	it('is exactly the seven sectors the design names, in order', () => {
		expect(SETTINGS_TABS.map((t) => t.id)).toEqual([
			'connection',
			'proxmox',
			'subsystems',
			'guests',
			'monitoring',
			'tokens',
			'about',
		]);
	});

	it('addresses every sector with ?tab= on the settings route', () => {
		const tabs = queryTabs(SETTINGS_TABS, '/ui/settings');
		expect(tabs.map((t) => t.href)).toEqual([
			'/ui/settings?tab=connection',
			'/ui/settings?tab=proxmox',
			'/ui/settings?tab=subsystems',
			'/ui/settings?tab=guests',
			'/ui/settings?tab=monitoring',
			'/ui/settings?tab=tokens',
			'/ui/settings?tab=about',
		]);
	});

	it('selects the tab the URL names, and falls back to Connection', () => {
		const at = (q: string) => activeQueryTab(SETTINGS_TABS, new URL('http://x/ui/settings' + q));
		expect(at('?tab=tokens')).toBe('tokens');
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

	it('names a tab that exists', () => {
		const ids = new Set(SETTINGS_TABS.map((t) => t.id));
		for (const [hash] of CASES) {
			expect(ids.has(settingsTabFromHash(hash))).toBe(true);
		}
	});
});
