import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';

// ---------------------------------------------------------------------------
// The design-token gate.
//
// src/app.css is the single place colour and type are decided. This file reads
// THAT file (not a copy of its values) and holds it to three rules:
//
//   1. Contrast — every text/background pair the UI actually paints clears
//      WCAG AA, in BOTH themes (Night, the bare :root, and Day, the
//      :root[data-theme="day"] block). Pairs are derived from the component
//      rules themselves, so a new badge or button variant is gated the moment
//      it is written, not when someone remembers to add it here.
//   2. Type voice — page titles are the display serif, reading text the
//      serif, chrome the sans, and numbers / tags / machine values the mono.
//   3. Fonts — every face the stacks lead with is self-hosted (the CSP is
//      default-src 'self'), its file exists, and its OFL licence ships beside it.
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

/** Every custom property declared in the block opened by `selector`. */
function readBlock(selector: RegExp, what: string): Record<string, string> {
	const block = CSS.match(new RegExp(selector.source + '\\s*\\{([\\s\\S]*?)\\n\\}'));
	expect(block, `${what} token block not found in app.css`).toBeTruthy();
	const out: Record<string, string> = {};
	for (const m of block![1].matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
		out[m[1]] = m[2].trim();
	}
	return out;
}

/** Night: the bare `:root` block (the default theme). */
const NIGHT = readBlock(/(?:^|\n):root(?!\[)/, 'Night (:root)');
/** Day: its own colours over Night's non-colour tokens (type, space, radius). */
const DAY_OWN = readBlock(/:root\[data-theme="day"\]/, 'Day (:root[data-theme="day"])');
const DAY = { ...NIGHT, ...DAY_OWN };
const THEMES: Array<[string, Record<string, string>]> = [
	['night', NIGHT],
	['day', DAY],
];

/** Every rule inside @layer components, as selector -> declarations. */
function readComponentRules(): { selector: string; decls: string }[] {
	const layer = CSS.slice(CSS.indexOf('@layer components'));
	const rules: { selector: string; decls: string }[] = [];
	for (const m of layer.matchAll(/\n\t(\.[^{}\n]+?)\s*\{([^{}]*)\}/g)) {
		rules.push({ selector: m[1].trim(), decls: m[2] });
	}
	return rules;
}

// The non-theme assertions (contract, type voice) read Night, the default.
const T = NIGHT;
const RULES = readComponentRules();

function hex(tokenRef: string, tokens: Record<string, string> = T): string | null {
	const m = tokenRef.match(/var\((--[a-z0-9-]+)\)/);
	const raw = m ? tokens[m[1]] : tokenRef.trim();
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

	for (const [theme, tokens] of THEMES) {
		it(`${theme}: every --color-* token is a parseable hex colour`, () => {
			for (const [name, value] of Object.entries(tokens)) {
				if (!name.startsWith('--color-') && !name.startsWith('--chart-')) continue;
				expect(/^#[0-9a-f]{3,8}$/i.test(value), `${name} is not a hex colour: ${value}`).toBe(true);
			}
		});
	}

	// A colour Day forgets keeps its Night value: near-black ink on paper, or
	// a near-black panel on a light field. Every colour is redeclared.
	it('Day redeclares every colour token Night declares', () => {
		const colours = Object.keys(NIGHT).filter((n) => n.startsWith('--color-') || n.startsWith('--chart-'));
		expect(colours.length).toBeGreaterThan(30);
		const missing = colours.filter((n) => !(n in DAY_OWN));
		expect(missing, `Day block is missing: ${missing.join(', ')}`).toEqual([]);
	});

	it('the two themes say which they are to the browser (form controls, scrollbars)', () => {
		expect(CSS).toMatch(/:root\s*\{\s*color-scheme:\s*dark;/);
		expect(CSS).toMatch(/:root\[data-theme="day"\]\s*\{\s*color-scheme:\s*light;/);
	});
});

describe('fonts: self-hosted, present, licensed (CSP default-src self)', () => {
	const STATIC = resolve(process.cwd(), 'static');
	const faces = [...CSS.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((m) => ({
		family: m[1].match(/font-family:\s*"([^"]+)"/)?.[1] ?? '',
		srcs: [...m[1].matchAll(/url\(([^)]+)\)/g)].map((u) => u[1].replace(/["']/g, '')),
	}));

	it('declares faces for the four families of the direction', () => {
		const families = new Set(faces.map((f) => f.family));
		expect([...families].sort()).toEqual(
			['Atkinson Hyperlegible', 'Instrument Serif', 'JetBrains Mono', 'Source Serif 4'].sort(),
		);
	});

	it('every url() in the stylesheet is a woff2 under /fonts that exists in static/', () => {
		const urls = [...CSS.matchAll(/url\(([^)]+)\)/g)].map((u) => u[1].replace(/["']/g, ''));
		expect(urls.length).toBeGreaterThanOrEqual(faces.length);
		for (const u of urls) {
			// Never a font service, never a protocol-relative or absolute URL.
			expect(u, `${u} is not self-hosted`).toMatch(/^\/fonts\/[a-z0-9-]+\.woff2$/);
			expect(existsSync(resolve(STATIC, u.slice(1))), `${u} is missing from static/`).toBe(true);
		}
		expect(CSS).not.toMatch(/@import/);
	});

	it('ships the OFL licence of every family beside the files', () => {
		const licences: Record<string, string> = {
			'Atkinson Hyperlegible': 'OFL-AtkinsonHyperlegible.txt',
			'Instrument Serif': 'OFL-InstrumentSerif.txt',
			'JetBrains Mono': 'OFL-JetBrainsMono.txt',
			'Source Serif 4': 'OFL-SourceSerif4.txt',
		};
		for (const [family, file] of Object.entries(licences)) {
			const path = resolve(STATIC, 'fonts', file);
			expect(existsSync(path), `${family}: ${file} missing`).toBe(true);
			expect(readFileSync(path, 'utf8'), `${family}: not the OFL`).toMatch(
				/SIL OPEN FONT LICENSE Version 1\.1/,
			);
		}
	});

	// The identity system's tell: system-ui / Inter as body text. Each stack
	// leads with its self-hosted face and falls back to named families only.
	it('each stack leads with its self-hosted face and never names system-ui or Inter', () => {
		const lead: Record<string, string> = {
			'--font-display': 'Instrument Serif',
			'--font-serif': 'Source Serif 4',
			'--font-sans': 'Atkinson Hyperlegible',
			'--font-mono': 'JetBrains Mono',
		};
		for (const [token, family] of Object.entries(lead)) {
			expect(T[token], token).toMatch(new RegExp(`^"${family}"`));
			expect(T[token], token).not.toMatch(/system-ui|ui-sans-serif|-apple-system|\bInter\b/);
		}
		expect(T['--font-serif']).toMatch(/serif$/);
		expect(T['--font-display']).toMatch(/serif$/);
		expect(T['--font-sans']).toMatch(/sans-serif$/);
		expect(T['--font-mono']).toMatch(/monospace$/);
	});
});

// The kill list (identity system): no background gradients anywhere, the
// "parked" lamp included.
describe('no gradients', () => {
	it('app.css paints no gradient', () => {
		expect(CSS).not.toMatch(/gradient\(/);
	});
});

// Every contrast rule runs once per theme. A pair that passes in Night and
// fails in Day is a page an operator cannot read on a bright desk.
// The derived component pairs are found once (they are theme-independent:
// a rule names tokens, the theme decides their values).
const PAIRS = RULES.map((r) => ({
	selector: r.selector,
	fg: decl(r.decls, 'color'),
	bg: decl(r.decls, 'background'),
	// `transparent` carries no colour of its own — .btn-ghost is checked
	// against the fields it actually sits on, in its own test below.
})).filter((p) => p.fg && p.bg && p.bg !== 'transparent');

describe('contrast: the derived component pairs are actually found', () => {
	it('finds the badge / tag / button / input pairs to check', () => {
		// A tripwire: if a refactor stops this parser from seeing the rules, the
		// suite would go green while checking nothing.
		expect(PAIRS.length).toBeGreaterThanOrEqual(10);
		const selectors = PAIRS.map((p) => p.selector).join(' ');
		for (const s of ['.badge-proposed', '.badge-applied', '.btn-primary', '.input', '.tag', '.tag-crit']) {
			expect(selectors, `${s} is not being contrast-checked`).toContain(s);
		}
	});
});

for (const [theme, TT] of THEMES) {
	describe(`${theme}: contrast: text on the field (WCAG AA)`, () => {
		const FIELDS = {
			bg: TT['--color-bg'],
			surface: TT['--color-surface'],
			raised: TT['--color-surface-raised'],
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
					const r = contrastRatio(TT[ink], field);
					expect(r, `${theme}: ${ink} on --color-${fieldName} was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(
						AA_NORMAL,
					);
				});
			}
		}
	});

	describe(`${theme}: contrast: component classes carry their own background (WCAG AA)`, () => {
		// Derived from app.css itself: any rule that sets BOTH a colour and a
		// background is a text/background pair the user will actually see.
		for (const p of PAIRS) {
			it(`${p.selector} foreground on its own background >= ${AA_NORMAL}:1`, () => {
				const fg = hex(p.fg!, TT);
				const bg = hex(p.bg!, TT);
				expect(fg, `${p.selector}: colour ${p.fg} does not resolve to a hex token`).toBeTruthy();
				expect(bg, `${p.selector}: background ${p.bg} does not resolve to a hex token`).toBeTruthy();
				const r = contrastRatio(fg!, bg!);
				expect(r, `${theme}: ${p.selector} was ${r.toFixed(2)}:1 (${fg} on ${bg})`).toBeGreaterThanOrEqual(
					AA_NORMAL,
				);
			});
		}

		it('.btn-ghost is transparent, so its ink is checked against every field', () => {
			const ghost = RULES.find((r) => r.selector === '.btn-ghost');
			expect(ghost).toBeTruthy();
			expect(decl(ghost!.decls, 'background')).toBe('transparent');
			const ink = hex(decl(ghost!.decls, 'color')!, TT)!;
			for (const f of ['--color-bg', '--color-surface', '--color-surface-raised']) {
				expect(contrastRatio(ink, TT[f]), `${theme}: ghost on ${f}`).toBeGreaterThanOrEqual(AA_NORMAL);
			}
		});
	});

	describe(`${theme}: contrast: tinted chips painted from route markup`, () => {
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
				const r = contrastRatio(TT[ink], TT[tint]);
				expect(r, `${theme}: ${ink} on ${tint} was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA_NORMAL);
			});
		}
	});

	describe(`${theme}: contrast: non-text (WCAG 1.4.11, 3:1)`, () => {
		const FIELDS = ['--color-bg', '--color-surface', '--color-surface-raised'];

		it('control boundaries are visible on every field they sit on', () => {
			for (const field of FIELDS) {
				const r = contrastRatio(TT['--color-border-strong'], TT[field]);
				expect(r, `${theme}: --color-border-strong on ${field} was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(
					AA_LARGE,
				);
			}
		});

		// The focus ring is the accent; the lamps are ok / warn / danger and the
		// hollow no-data ring. Each is a boundary the eye must find on any field.
		it('the focus ring and every lamp are visible on every field', () => {
			for (const t of ['--color-accent', '--color-ok', '--color-warn', '--color-danger', '--color-nodata']) {
				for (const field of FIELDS) {
					const r = contrastRatio(TT[t], TT[field]);
					expect(r, `${theme}: ${t} on ${field} was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA_LARGE);
				}
			}
		});

		it('chart segments are distinguishable from the panel they sit on', () => {
			const charts = Object.entries(TT).filter(([n]) => n.startsWith('--chart-'));
			expect(charts.length).toBe(6);
			for (const [name, value] of charts) {
				const r = contrastRatio(value, TT['--color-surface']);
				expect(r, `${theme}: ${name} on --color-surface was ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA_LARGE);
			}
		});
	});
}

describe('type voice: display titles, serif reads, sans works, mono counts', () => {
	function fontOf(selector: string): string {
		const rule = RULES.find((r) => r.selector === selector);
		expect(rule, `${selector} is not defined in app.css`).toBeTruthy();
		const f = decl(rule!.decls, 'font-family');
		expect(f, `${selector} declares no font-family`).toBeTruthy();
		return f!;
	}

	// The display serif (Instrument Serif) is for the page title and the
	// wordmark ONLY: it is a condensed display face, unreadable below ~20px.
	for (const s of ['.page-title', '.wordmark']) {
		it(`${s} is the display serif`, () => expect(fontOf(s)).toBe('var(--font-display)'));
	}
	it('nothing else is set in the display serif', () => {
		const display = RULES.filter((r) => decl(r.decls, 'font-family') === 'var(--font-display)');
		expect(display.map((r) => r.selector).sort()).toEqual(['.page-title', '.wordmark']);
	});

	// Reading text — descriptions, empty states, attention sentences, KB and
	// journal content - is Source Serif 4.
	for (const s of ['.prose-note', '.prose-body']) {
		it(`${s} is serif`, () => expect(fontOf(s)).toBe('var(--font-serif)'));
	}

	// UI chrome — nav, controls, labels, table text, section and panel titles.
	for (const s of ['.section-title', '.panel-title', '.field-label', '.maker-mark', '.data-table', '.btn', '.input']) {
		it(`${s} is sans`, () => expect(fontOf(s)).toBe('var(--font-sans)'));
	}

	// Numbers compared down a column, tags, and machine values are mono:
	// every digit is one width, so a change in a column shows.
	it('exactly the number, tag and machine classes are mono', () => {
		const monoRules = RULES.filter((r) => decl(r.decls, 'font-family') === 'var(--font-mono)');
		expect(monoRules.map((r) => r.selector).sort()).toEqual(
			['.badge', '.code-block', '.metric', '.num', '.num-inline', '.panel-meta', '.tag'].sort(),
		);
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
