import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// ---------------------------------------------------------------------------
// The design-token gate.
//
// src/app.css is the single place colour and type are decided. This file reads
// THAT file (not a copy of its values) and holds it to two rules:
//
//   1. Contrast — every text/background pair the UI actually paints clears
//      WCAG AA. Pairs are derived from the component rules themselves, so a new
//      badge or button variant is gated the moment it is written, not when
//      someone remembers to add it here.
//   2. Type voice — reading text resolves to the serif token, UI chrome and
//      data resolve to the sans token, and only machine output gets mono.
//
// Change a token to a failing value and this test goes red. That is the point:
// the palette cannot silently regress.
// ---------------------------------------------------------------------------

// Read from disk, not through an import: the assertions must hold against the
// file a developer edits, byte for byte.
const CSS = readFileSync(resolve(process.cwd(), 'src/app.css'), 'utf8');

const AA_NORMAL = 4.5; // WCAG 1.4.3, text below 18.66px bold / 24px
const AA_LARGE = 3.0; // WCAG 1.4.3 large text, and 1.4.11 non-text contrast

/** Relative luminance, WCAG 2.x definition. */
function luminance(hex: string): number {
	const h = hex.replace('#', '').trim();
	const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
	const chan = (i: number) => {
		const v = parseInt(full.slice(i * 2, i * 2 + 2), 16) / 255;
		return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
	};
	return 0.2126 * chan(0) + 0.7152 * chan(1) + 0.0722 * chan(2);
}

/** WCAG contrast ratio between two opaque colours, 1..21. */
export function contrastRatio(fg: string, bg: string): number {
	const a = luminance(fg);
	const b = luminance(bg);
	const [hi, lo] = a > b ? [a, b] : [b, a];
	return (hi + 0.05) / (lo + 0.05);
}

/** Every custom property declared on :root, as name -> value. */
function readTokens(): Record<string, string> {
	const block = CSS.match(/:root\s*\{([\s\S]*?)\n\}/);
	expect(block, ':root token block not found in app.css').toBeTruthy();
	const out: Record<string, string> = {};
	for (const m of block![1].matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
		out[m[1]] = m[2].trim();
	}
	return out;
}

/** Every rule inside @layer components, as selector -> declarations. */
function readComponentRules(): { selector: string; decls: string }[] {
	const layer = CSS.slice(CSS.indexOf('@layer components'));
	const rules: { selector: string; decls: string }[] = [];
	for (const m of layer.matchAll(/\n\t(\.[^{}\n]+?)\s*\{([^{}]*)\}/g)) {
		rules.push({ selector: m[1].trim(), decls: m[2] });
	}
	return rules;
}

const T = readTokens();
const RULES = readComponentRules();

function hex(tokenRef: string): string | null {
	const m = tokenRef.match(/var\((--[a-z0-9-]+)\)/);
	const raw = m ? T[m[1]] : tokenRef.trim();
	return raw && /^#[0-9a-f]{3,8}$/i.test(raw.trim()) ? raw.trim() : null;
}

function decl(decls: string, prop: string): string | null {
	const m = decls.match(new RegExp(`(?:^|[;{\\s])${prop}\\s*:\\s*([^;]+);`));
	return m ? m[1].trim() : null;
}

describe('design tokens: the palette exists and is well formed', () => {
	// The names below are a CONTRACT shared with the friend portal. Renaming one
	// here silently breaks the other surface, so the set is asserted, not assumed.
	const CONTRACT = [
		'--font-serif',
		'--font-sans',
		'--color-bg',
		'--color-surface',
		'--color-border',
		'--color-text',
		'--color-muted',
		'--color-accent',
		'--space-1',
		'--space-2',
		'--space-3',
		'--space-4',
		'--space-5',
		'--space-6',
		'--radius-sm',
		'--radius-md',
	];

	it('declares every contract token', () => {
		for (const name of CONTRACT) expect(T[name], `missing token ${name}`).toBeTruthy();
	});

	it('every --color-* token is a parseable hex colour', () => {
		for (const [name, value] of Object.entries(T)) {
			if (!name.startsWith('--color-') && !name.startsWith('--chart-')) continue;
			expect(/^#[0-9a-f]{3,8}$/i.test(value), `${name} is not a hex colour: ${value}`).toBe(true);
		}
	});

	it('serif stack needs no download — it ends in a generic family', () => {
		expect(T['--font-serif']).toMatch(/serif$/);
		expect(T['--font-serif']).toContain('Source Serif 4');
		// No @import / url() anywhere: the type voice must cost zero requests.
		expect(CSS).not.toMatch(/@import|url\(/);
	});
});

describe('contrast: text on the field (WCAG AA)', () => {
	const FIELDS = {
		bg: T['--color-bg'],
		surface: T['--color-surface'],
		raised: T['--color-surface-raised'],
	};
	// Every ink that route markup paints on a field, via text-* utilities.
	const INKS = [
		'--color-text',
		'--color-text-strong',
		'--color-muted',
		'--color-accent',
		'--color-accent-strong',
		'--color-ok',
		'--color-warn',
		'--color-danger',
		'--color-info',
		'--color-note',
	];

	for (const [fieldName, field] of Object.entries(FIELDS)) {
		for (const ink of INKS) {
			it(`${ink} on ${fieldName} >= ${AA_NORMAL}:1`, () => {
				const r = contrastRatio(T[ink], field);
				expect(r, `${ink} on --color-${fieldName} was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(
					AA_NORMAL,
				);
			});
		}
	}
});

describe('contrast: component classes carry their own background (WCAG AA)', () => {
	// Derived from app.css itself: any rule that sets BOTH a colour and a
	// background is a text/background pair the user will actually see.
	const pairs = RULES.map((r) => ({
		selector: r.selector,
		fg: decl(r.decls, 'color'),
		bg: decl(r.decls, 'background'),
		// `transparent` carries no colour of its own — .btn-ghost is checked
		// against the fields it actually sits on, in its own test below.
	})).filter((p) => p.fg && p.bg && p.bg !== 'transparent');

	it('finds the badge / button / input pairs to check', () => {
		// A tripwire: if a refactor stops this parser from seeing the rules, the
		// suite would go green while checking nothing.
		expect(pairs.length).toBeGreaterThanOrEqual(10);
		const selectors = pairs.map((p) => p.selector).join(' ');
		for (const s of ['.badge-proposed', '.badge-applied', '.btn-primary', '.input']) {
			expect(selectors, `${s} is not being contrast-checked`).toContain(s);
		}
	});

	for (const p of pairs) {
		it(`${p.selector} foreground on its own background >= ${AA_NORMAL}:1`, () => {
			const fg = hex(p.fg!);
			const bg = hex(p.bg!);
			expect(fg, `${p.selector}: colour ${p.fg} does not resolve to a hex token`).toBeTruthy();
			expect(bg, `${p.selector}: background ${p.bg} does not resolve to a hex token`).toBeTruthy();
			const r = contrastRatio(fg!, bg!);
			expect(r, `${p.selector} was ${r.toFixed(2)}:1 (${fg} on ${bg})`).toBeGreaterThanOrEqual(
				AA_NORMAL,
			);
		});
	}

	it('.btn-ghost is transparent, so its ink is checked against both fields', () => {
		const ghost = RULES.find((r) => r.selector === '.btn-ghost');
		expect(ghost).toBeTruthy();
		expect(decl(ghost!.decls, 'background')).toBe('transparent');
		const ink = hex(decl(ghost!.decls, 'color')!)!;
		expect(contrastRatio(ink, T['--color-bg'])).toBeGreaterThanOrEqual(AA_NORMAL);
		expect(contrastRatio(ink, T['--color-surface'])).toBeGreaterThanOrEqual(AA_NORMAL);
	});
});

describe('contrast: tinted chips painted from route markup', () => {
	// Utility pairs the routes compose by hand (source chips, drift chips, tab
	// counts, toasts). They never pass through a component rule, so the derived
	// check above cannot see them — they are listed here instead.
	const CHIPS: [string, string][] = [
		['--color-accent', '--color-accent-tint'],
		['--color-accent-strong', '--color-accent-tint'],
		['--color-ok', '--color-ok-tint'],
		['--color-warn', '--color-warn-tint'],
		['--color-danger', '--color-danger-tint'],
		['--color-info', '--color-info-tint'],
		['--color-note', '--color-note-tint'],
	];

	for (const [ink, tint] of CHIPS) {
		it(`${ink} on ${tint} >= ${AA_NORMAL}:1`, () => {
			const r = contrastRatio(T[ink], T[tint]);
			expect(r, `${ink} on ${tint} was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA_NORMAL);
		});
	}
});

describe('contrast: non-text (WCAG 1.4.11)', () => {
	it('control boundaries are visible on every field they sit on', () => {
		for (const field of ['--color-bg', '--color-surface', '--color-surface-raised']) {
			const r = contrastRatio(T['--color-border-strong'], T[field]);
			expect(r, `--color-border-strong on ${field} was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(
				AA_LARGE,
			);
		}
	});

	it('chart segments are distinguishable from the card they sit on', () => {
		for (const [name, value] of Object.entries(T)) {
			if (!name.startsWith('--chart-')) continue;
			const r = contrastRatio(value, T['--color-surface']);
			expect(r, `${name} on --color-surface was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA_LARGE);
		}
	});
});

describe('type voice: serif reads, sans works, mono only for machines', () => {
	function fontOf(selector: string): string {
		const rule = RULES.find((r) => r.selector === selector);
		expect(rule, `${selector} is not defined in app.css`).toBeTruthy();
		const f = decl(rule!.decls, 'font-family');
		expect(f, `${selector} declares no font-family`).toBeTruthy();
		return f!;
	}

	// Reading text — titles, descriptions, empty states, KB/journal content.
	// `.wordmark` belongs here: the product's name is identity, not chrome, and
	// it is the one place the estate's type voice should be unmistakable.
	for (const s of ['.page-title', '.section-title', '.prose-note', '.prose-body', '.wordmark']) {
		it(`${s} is serif`, () => expect(fontOf(s)).toBe('var(--font-serif)'));
	}

	// UI chrome and data — nav, controls, table headers, numbers.
	for (const s of ['.field-label', '.maker-mark', '.data-table', '.num', '.metric', '.badge', '.btn', '.input']) {
		it(`${s} is sans`, () => expect(fontOf(s)).toBe('var(--font-sans)'));
	}

	it('.code-block is the only mono class', () => {
		expect(fontOf('.code-block')).toBe('var(--font-mono)');
		const monoRules = RULES.filter((r) => decl(r.decls, 'font-family') === 'var(--font-mono)');
		expect(monoRules.map((r) => r.selector)).toEqual(['.code-block']);
	});

	it('data classes line their numerals up', () => {
		for (const s of ['.data-table', '.num', '.metric']) {
			const rule = RULES.find((r) => r.selector === s)!;
			expect(decl(rule.decls, 'font-variant-numeric'), `${s} must be tabular`).toBe('tabular-nums');
		}
	});

	it('reading text keeps a calm line-height (1.6-1.7)', () => {
		for (const s of ['.prose-note', '.prose-body']) {
			const rule = RULES.find((r) => r.selector === s)!;
			const lh = Number(decl(rule.decls, 'line-height'));
			expect(lh, `${s} line-height ${lh}`).toBeGreaterThanOrEqual(1.6);
			expect(lh).toBeLessThanOrEqual(1.7);
		}
	});
});
