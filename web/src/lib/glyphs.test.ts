import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

/**
 * THE GLYPH GATE (UI slice 0, review F8, UI plan 4.8).
 *
 * Text glyphs stood in for icons - refresh arrows, crosses, ticks, chevrons,
 * a hamburger, a warning sign. They render in whatever font the platform has,
 * at whatever weight it chooses, and they are the "emoji as icon" tell the
 * identity system names, one step removed. Every one became an <Icon> from
 * the inline SVG set; this gate keeps them out.
 *
 * What it reads: every .svelte and non-test .ts file under src/routes and
 * src/lib, plus app.html - the code that renders the console - with comments
 * stripped (a comment that says "login → app" renders nothing). What it
 * forbids: arrows, technical symbols, geometric shapes, dingbats, the
 * supplemental arrow blocks, and anything with emoji presentation.
 *
 * Teeth: put `↻ Refresh` back on any button (or an emoji anywhere in markup)
 * and this fails naming the file and line.
 */

const FORBIDDEN =
	/[←-⇿⌀-⏿■-➿⟀-⟿⤀-⥿⬀-⯿]|\p{Extended_Pictographic}/u;
// Extended_Pictographic includes the copyright and registered signs, which
// are typography rather than icons. Neither is used today; if one is, it is
// allowed here on purpose rather than by accident.
const ALLOWED = new Set(['©', '®']);

const WEB = process.cwd();
const ROOTS = [join(WEB, 'src', 'routes'), join(WEB, 'src', 'lib')];

function sourceFiles(dir: string): string[] {
	const out: string[] = [];
	for (const entry of readdirSync(dir)) {
		const full = join(dir, entry);
		if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
		else if (entry.endsWith('.svelte')) out.push(full);
		else if (entry.endsWith('.ts') && !entry.endsWith('.test.ts')) out.push(full);
	}
	return out;
}

/** Blank out comments, keeping line numbers. */
export function stripComments(src: string): string {
	const blank = (m: string) => m.replace(/[^\n]/g, ' ');
	return src
		.replace(/<!--[\s\S]*?-->/g, blank)
		.replace(/\/\*[\s\S]*?\*\//g, blank)
		.replace(/(^|[\s;{}(),])\/\/[^\n]*/g, (m, lead: string) => lead + blank(m.slice(lead.length)));
}

/** `file:line: text` for every forbidden glyph outside a comment. */
export function glyphHits(name: string, src: string): string[] {
	const hits: string[] = [];
	stripComments(src)
		.split('\n')
		.forEach((line, i) => {
			for (const ch of line) {
				if (FORBIDDEN.test(ch) && !ALLOWED.has(ch)) {
					hits.push(`${name}:${i + 1}: ${line.trim().slice(0, 80)}`);
					break;
				}
			}
		});
	return hits;
}

describe('no glyph or emoji stands in for an icon', () => {
	it('the matcher bites, and ignores comments', () => {
		// Proven on samples, so a broken pattern cannot turn the gate into a
		// formality.
		expect(glyphHits('x', '<button>↻ Refresh</button>')).toHaveLength(1);
		expect(glyphHits('x', '<a>Next →</a>')).toHaveLength(1);
		expect(glyphHits('x', "{open ? '▾' : '▸'}")).toHaveLength(1);
		expect(glyphHits('x', '<span>☰</span>')).toHaveLength(1);
		expect(glyphHits('x', 'health = `✓ ok`;')).toHaveLength(1);
		expect(glyphHits('x', '<b>\u{1F680} launch</b>')).toHaveLength(1);
		expect(glyphHits('x', '// login → app\n<!-- a → b -->\n/* ✓ */')).toEqual([]);
		// A URL's `//` is not a comment.
		expect(glyphHits('x', '<a href="https://x.example/">→</a>')).toHaveLength(1);
	});

	it('no rendering source under src/routes, src/lib or app.html carries one', () => {
		const files = [...ROOTS.flatMap(sourceFiles), join(WEB, 'src', 'app.html')];
		// A tripwire: a walk that finds nothing passes vacuously.
		expect(files.length).toBeGreaterThan(50);
		const hits = files.flatMap((f) => glyphHits(relative(WEB, f), readFileSync(f, 'utf8')));
		expect(hits, hits.join('\n')).toEqual([]);
	});
});
