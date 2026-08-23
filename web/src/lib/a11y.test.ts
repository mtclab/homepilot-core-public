import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Every form control is reachable by name (#445 B5).
 *
 * Eleven `<label>` elements sat next to their control with nothing tying them
 * together, so a screen reader announced "edit text, blank" and clicking the
 * label did not focus the field. svelte-check had been reporting all eleven as
 * warnings for as long as they existed - the web gate only failed on ERRORS, so
 * the warnings were a note nobody had to act on. `make gate-web` now runs
 * `svelte-check --threshold warning`, which is the real fix.
 *
 * This test is the second half: it fails on the specific shape that regressed,
 * independently of whether anyone kept the threshold flag. A label that names a
 * control but contains neither the control nor a `for=` is the bug.
 */
// `import.meta.url` is not a file: URL under the browser-ish test environment, so
// the routes directory is resolved from the vitest working directory (web/).
const ROUTES = join(process.cwd(), 'src', 'routes');

function svelteFiles(dir: string): string[] {
	const out: string[] = [];
	for (const entry of readdirSync(dir)) {
		const full = join(dir, entry);
		if (statSync(full).isDirectory()) out.push(...svelteFiles(full));
		else if (entry.endsWith('.svelte')) out.push(full);
	}
	return out;
}

/** `<label ...>` openings that carry neither a `for=` nor an enclosed control. */
function orphanLabels(source: string): string[] {
	const orphans: string[] = [];
	const re = /<label\b([^>]*)>([\s\S]*?)<\/label>/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(source)) !== null) {
		const [, attrs, body] = m;
		if (/\bfor=/.test(attrs)) continue;
		if (/<(input|select|textarea)\b/.test(body)) continue;
		orphans.push(body.trim().slice(0, 60));
	}
	return orphans;
}

describe('form labels', () => {
	it('every label either wraps its control or names it with for=', () => {
		const offenders: string[] = [];
		for (const file of svelteFiles(ROUTES)) {
			const found = orphanLabels(readFileSync(file, 'utf8'));
			for (const label of found) {
				offenders.push(`${file.replace(ROUTES, 'routes')}: "${label}"`);
			}
		}

		expect(offenders, offenders.join('\n')).toEqual([]);
	});
});
