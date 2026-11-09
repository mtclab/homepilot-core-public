import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage, goto } from '../../lib/test-mocks';
import SettingsPage from './+page.svelte';
import { SETTINGS_TABS } from '$lib/settingsTabs';

/**
 * Settings' sectors are addressable and keyboard-reachable (#549 F6).
 *
 * Teeth: make the tab local component state and both the "?tab= selects" and
 * the href cases fail; drop the legacy-fragment redirect and the deep-link case
 * fails; render the panel without its tab roles and the a11y case fails.
 */
/** The tab the rendered page says is current. */
function currentTab(): string {
	const selected = screen
		.getAllByRole('tab')
		.filter((t) => t.getAttribute('aria-selected') === 'true');
	expect(selected).toHaveLength(1);
	return selected[0].textContent ?? '';
}

describe('Settings tab addressing', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings');
	});

	it('renders one link per sector, each carrying its own ?tab=', () => {
		render(SettingsPage);
		const tabs = screen.getAllByRole('tab');
		expect(tabs.map((t) => t.textContent)).toEqual(SETTINGS_TABS.map((t) => t.label));
		expect(tabs.map((t) => t.getAttribute('href'))).toEqual(
			SETTINGS_TABS.map((t) => `/ui/settings?tab=${t.id}`),
		);
	});

	it('an address-less /settings opens Connection', () => {
		render(SettingsPage);
		expect(currentTab()).toBe('Connection');
		expect(screen.getByRole('heading', { name: 'API Connection' })).toBeTruthy();
		// And only that sector: the six others are not stacked underneath it.
		expect(screen.queryByRole('heading', { name: 'Proxmox Connection' })).toBeNull();
	});

	for (const [tab, heading] of [
		['proxmox', 'Proxmox Connection'],
		['about', 'About'],
	] as Array<[string, string]>) {
		it(`?tab=${tab} opens ${heading}`, async () => {
			setPage(`/ui/settings?tab=${tab}`);
			render(SettingsPage);
			await waitFor(() => expect(screen.getByRole('heading', { name: heading })).toBeTruthy());
			expect(screen.queryByRole('heading', { name: 'API Connection' })).toBeNull();
		});
	}

	// UI slice 1: these left Settings. Moved, not duplicated - the page that
	// renders a sector must not ALSO render its panels here.
	it('no longer renders the Monitoring, Guests or Tokens panels', () => {
		render(SettingsPage);
		const labels = screen.getAllByRole('tab').map((t) => t.textContent);
		for (const gone of ['Monitoring', 'Guests', 'Tokens']) expect(labels).not.toContain(gone);
	});

	it('gives the panel the tab roles a screen reader needs', () => {
		setPage('/ui/settings?tab=about');
		render(SettingsPage);
		const panel = document.getElementById('settings-panel');
		expect(panel?.getAttribute('role')).toBe('tabpanel');
		expect(panel?.getAttribute('aria-labelledby')).toBe('tab-about');
		expect(screen.getByRole('tablist', { name: 'Settings sections' })).toBeTruthy();
		// Exactly one tab is in the tab order (roving tabindex).
		const reachable = screen.getAllByRole('tab').filter((t) => t.getAttribute('tabindex') === '0');
		expect(reachable).toHaveLength(1);
		expect(reachable[0].textContent).toBe('About');
	});
});

describe('Legacy Settings deep links keep working', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('#proxmox is rewritten to the canonical ?tab=proxmox', async () => {
		setPage('/ui/settings#proxmox');
		render(SettingsPage);
		await waitFor(() => expect(goto).toHaveBeenCalled());
		const [target, opts] = (goto as ReturnType<typeof vi.fn>).mock.calls[0];
		expect(target).toBe('/ui/settings?tab=proxmox');
		expect(opts).toMatchObject({ replaceState: true });
	});

	it('an explicit ?tab= wins over a stale fragment', async () => {
		setPage('/ui/settings?tab=about#tokens');
		render(SettingsPage);
		expect(currentTab()).toBe('About');
		expect(goto).not.toHaveBeenCalled();
	});

	it('a fragment Settings never had causes no redirect', async () => {
		setPage('/ui/settings#somewhere-else');
		render(SettingsPage);
		expect(currentTab()).toBe('Connection');
		expect(goto).not.toHaveBeenCalled();
	});
});
