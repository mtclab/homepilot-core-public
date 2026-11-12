import { render, screen, cleanup, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import ThemeControl from './components/ThemeControl.svelte';
import { THEME_KEY, DEFAULT_PREF, readPref, resolveTheme, type ThemePref } from './theme';

/**
 * The theme control (UI slice 0; owner decision: Night default, Day, System).
 *
 * The goal is what the operator sees: choosing a theme repaints the page
 * (the `data-theme` the stylesheet keys off), the choice survives a reload,
 * System follows the OS, and the pre-paint script agrees with all of it so
 * a reload does not flash the wrong field.
 *
 * Teeth: stop writing localStorage in setPref and the "persists" case fails;
 * resolve System to Night unconditionally and the System cases fail; change
 * the key or the default in only one of theme.ts / theme-boot.js and the
 * parity cases fail.
 */

const html = document.documentElement;
let prefersLight = false;
const listeners = new Set<() => void>();

function stubMatchMedia() {
	window.matchMedia = vi.fn().mockImplementation((q: string) => ({
		matches: q.includes('light') ? prefersLight : !prefersLight,
		media: q,
		addEventListener: (_: string, fn: () => void) => listeners.add(fn),
		removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
	})) as unknown as typeof window.matchMedia;
}

beforeEach(() => {
	window.localStorage.clear();
	html.removeAttribute('data-theme');
	html.removeAttribute('data-theme-pref');
	prefersLight = false;
	listeners.clear();
	stubMatchMedia();
});
afterEach(() => cleanup());

describe('the theme control', () => {
	it('defaults to Night when nothing was chosen', () => {
		render(ThemeControl);
		expect(DEFAULT_PREF).toBe('night');
		expect(html.getAttribute('data-theme')).toBe('night');
		expect(screen.getByRole('radio', { name: 'Night' })).toBeChecked();
	});

	it('offers exactly System, Night and Day as a labelled group', () => {
		render(ThemeControl);
		expect(screen.getByRole('group', { name: 'Theme' })).toBeInTheDocument();
		expect(screen.getAllByRole('radio').map((r) => r.getAttribute('value'))).toEqual([
			'system',
			'night',
			'day',
		]);
	});

	it('switches the painted theme when Day is chosen', async () => {
		render(ThemeControl);
		await fireEvent.click(screen.getByRole('radio', { name: 'Day' }));
		expect(html.getAttribute('data-theme')).toBe('day');
		expect(html.getAttribute('data-theme-pref')).toBe('day');
	});

	it('persists the choice across a reload', async () => {
		render(ThemeControl);
		await fireEvent.click(screen.getByRole('radio', { name: 'Day' }));
		cleanup();
		html.removeAttribute('data-theme');

		// A fresh mount is what a reload does.
		render(ThemeControl);
		expect(window.localStorage.getItem(THEME_KEY)).toBe('day');
		expect(screen.getByRole('radio', { name: 'Day' })).toBeChecked();
		expect(html.getAttribute('data-theme')).toBe('day');
	});

	it('System follows the OS: light OS paints Day, dark OS paints Night', async () => {
		prefersLight = true;
		render(ThemeControl);
		await fireEvent.click(screen.getByRole('radio', { name: 'System' }));
		expect(html.getAttribute('data-theme')).toBe('day');
		expect(html.getAttribute('data-theme-pref')).toBe('system');

		prefersLight = false;
		await fireEvent.click(screen.getByRole('radio', { name: 'Day' }));
		await fireEvent.click(screen.getByRole('radio', { name: 'System' }));
		expect(html.getAttribute('data-theme')).toBe('night');
	});

	it('System repaints live when the OS switches, without a reload', async () => {
		render(ThemeControl);
		await fireEvent.click(screen.getByRole('radio', { name: 'System' }));
		expect(html.getAttribute('data-theme')).toBe('night');
		prefersLight = true;
		for (const fn of listeners) fn();
		expect(html.getAttribute('data-theme')).toBe('day');
	});

	it('an explicit choice ignores the OS', async () => {
		prefersLight = true;
		render(ThemeControl);
		await fireEvent.click(screen.getByRole('radio', { name: 'Night' }));
		for (const fn of listeners) fn();
		expect(html.getAttribute('data-theme')).toBe('night');
	});

	it('survives storage that throws (a private window), painting the default', () => {
		const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
			throw new Error('blocked');
		});
		try {
			expect(readPref()).toBe('night');
			render(ThemeControl);
			expect(html.getAttribute('data-theme')).toBe('night');
		} finally {
			get.mockRestore();
		}
	});
});

describe('the pre-paint script agrees with the control', () => {
	const BOOT = readFileSync(resolve(process.cwd(), 'static/theme-boot.js'), 'utf8');
	const runBoot = () => new Function(BOOT)();

	it('is wired into app.html as an external script (script-src self)', () => {
		const app = readFileSync(resolve(process.cwd(), 'src/app.html'), 'utf8');
		expect(app).toMatch(/<script src="%sveltekit\.assets%\/theme-boot\.js"><\/script>/);
		// No inline script: the CSP would refuse it.
		expect(app.match(/<script(?![^>]*\bsrc=)[^>]*>/g)).toBeNull();
	});

	it('reads the same storage key as theme.ts', () => {
		expect(BOOT).toContain(`'${THEME_KEY}'`);
	});

	const CASES: Array<[ThemePref | null | 'garbage', boolean]> = [
		[null, false],
		[null, true],
		['garbage', true],
		['night', true],
		['day', false],
		['system', true],
		['system', false],
	];
	for (const [stored, light] of CASES) {
		it(`stored=${stored} os=${light ? 'light' : 'dark'} paints what the control would`, () => {
			if (stored !== null) window.localStorage.setItem(THEME_KEY, stored);
			prefersLight = light;
			runBoot();
			const pref = readPref();
			expect(html.getAttribute('data-theme-pref')).toBe(pref);
			expect(html.getAttribute('data-theme')).toBe(resolveTheme(pref, light));
		});
	}
});
