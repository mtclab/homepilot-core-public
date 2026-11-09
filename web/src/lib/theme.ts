// The theme control (owner decision 2026-11-08: Night by default, Day, and
// System). A per-browser convenience, so it lives in localStorage - the one
// legitimate use the UI plan names - and never on the server.
//
// Two attributes on <html>:
//   data-theme       the theme actually painted: 'night' | 'day'. app.css keys
//                    its Day block off this; Night is the bare :root.
//   data-theme-pref  what the operator chose: 'system' | 'night' | 'day'.
//
// `system` follows prefers-color-scheme, live: switching the OS to light at
// dusk-to-dawn repaints without a reload. Before first paint the same
// resolution runs from static/theme-boot.js (an external script, because the
// CSP is script-src 'self' and an inline one would be refused); its key, its
// default and its resolution are asserted equal to this file's by
// src/lib/theme.test.ts.

export type ThemePref = 'system' | 'night' | 'day';
export type Theme = 'night' | 'day';

export const THEME_KEY = 'hp-theme';
export const DEFAULT_PREF: ThemePref = 'night';

export const THEME_CHOICES: ReadonlyArray<{ id: ThemePref; label: string }> = [
	{ id: 'system', label: 'System' },
	{ id: 'night', label: 'Night' },
	{ id: 'day', label: 'Day' },
];

function isPref(v: unknown): v is ThemePref {
	return v === 'system' || v === 'night' || v === 'day';
}

/** The stored choice, or the default when none (or garbage) is stored, or
 *  when storage itself is unavailable (a private window can throw). */
export function readPref(): ThemePref {
	try {
		const v = window.localStorage.getItem(THEME_KEY);
		return isPref(v) ? v : DEFAULT_PREF;
	} catch {
		return DEFAULT_PREF;
	}
}

function systemPrefersLight(): boolean {
	try {
		return window.matchMedia('(prefers-color-scheme: light)').matches;
	} catch {
		return false;
	}
}

/** What a choice paints. Only `system` asks the OS. */
export function resolveTheme(pref: ThemePref, prefersLight = systemPrefersLight()): Theme {
	if (pref === 'system') return prefersLight ? 'day' : 'night';
	return pref;
}

/** Paint a choice onto <html>. */
export function applyTheme(pref: ThemePref, root: HTMLElement = document.documentElement): Theme {
	const theme = resolveTheme(pref);
	root.setAttribute('data-theme', theme);
	root.setAttribute('data-theme-pref', pref);
	return theme;
}

/** Remember a choice for this browser and paint it. A failed write (storage
 *  blocked) still paints: the choice holds for this page view. */
export function setPref(pref: ThemePref): Theme {
	try {
		window.localStorage.setItem(THEME_KEY, pref);
	} catch {
		/* storage unavailable - the choice lasts for this view only */
	}
	return applyTheme(pref);
}

/**
 * Follow the OS while the choice is `system`. Returns the unsubscribe. Reads
 * the stored choice on every change rather than closing over one, so a choice
 * made after subscribing is honoured.
 */
export function watchSystemTheme(onChange: (t: Theme) => void = () => {}): () => void {
	let mql: MediaQueryList;
	try {
		mql = window.matchMedia('(prefers-color-scheme: light)');
	} catch {
		return () => {};
	}
	const handler = () => {
		if (readPref() === 'system') onChange(applyTheme('system'));
	};
	mql.addEventListener?.('change', handler);
	return () => mql.removeEventListener?.('change', handler);
}
