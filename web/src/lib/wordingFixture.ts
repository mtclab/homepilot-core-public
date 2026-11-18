// Test-only: the server's words for the rows and run lines the console's unit
// tests use. `wording.fixture.json` is GENERATED from the server's table
// (src/homepilot/dashboard/wording.py) and tests/test_what_happened_wording.py
// holds it equal to it - so these tests feed the Overview exactly what the
// server would send, and never a hand-written copy of its sentences.
import fixture from './wording.fixture.json';
import type { JournalWords, RunWords } from './journalWords';

interface RowKey {
	action: string;
	artifact_id?: string | null;
	target_host?: string | null;
}

interface FixturePhrase {
	verb: string;
	on: 'change' | 'host' | 'none';
	tail: string;
	many: string | null;
}

/**
 * The words the server sends with this journal row: a generated sample when
 * there is one (rows whose details change the words), else the server's phrase
 * for the code with the row's change or host as its object - the composition
 * `journal_words` makes. Throws for a code the server has no phrase for.
 */
export function journalWordsFor(row: RowKey): JournalWords {
	const hit = fixture.journal.find(
		(s) =>
			s.row.action === row.action &&
			(s.row.artifact_id ?? null) === (row.artifact_id ?? null) &&
			(s.row.target_host ?? null) === (row.target_host ?? null),
	);
	if (hit) return hit.words as JournalWords;
	const phrase = (fixture.phrases as Record<string, FixturePhrase>)[row.action];
	if (!phrase) throw new Error(`no server words for ${JSON.stringify(row)} - add it to SAMPLE_ROWS in tests/test_what_happened_wording.py`);
	const object =
		phrase.on === 'change' ? (row.artifact_id ?? 'a change') : phrase.on === 'host' ? (row.target_host ?? 'a host') : null;
	const text = phrase.verb + (object ? ` ${object}` : '') + phrase.tail;
	return { verb: phrase.verb, object, tail: phrase.tail, many: phrase.many, text };
}

/** The words the server sends with this Needs-you run line (`run_words`'s composition). */
export function runWordsFor(action: string, status: string, target: string): RunWords {
	const doing = (fixture.run_actions as Record<string, string>)[action];
	const said = (fixture.run_statuses as Record<string, string>)[status];
	if (!doing || !said) throw new Error(`no server words for run ${action}/${status}`);
	return { what: `${doing} ${target}`, status: said };
}
