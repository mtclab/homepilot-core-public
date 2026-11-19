// "What happened", in words (3.12; the words moved to the server in 3.14).
//
// The journal row says `withdraw` / `claude (hp_1a2b...)` / an artifact id;
// the operator reads "claude withdrew 4 approved changes". The words come from
// the server (`words` on each row, dashboard/wording.py) - the ONE table, which
// `hp status` and the assistant's get_audit_log read too, and which a gate test
// holds complete over every code the producers can write. This file only adds
// who did it ("You") and groups rows by the same actor doing the same thing
// one after another into one sentence with a count. It never shows a code.

import type { AuditEntry } from './api';

/** A piece of a sentence; `strong` is the thing acted on (a host, a change). */
export interface Part {
	text: string;
	strong?: boolean;
}

export interface Happening {
	key: string;
	/** Newest row of the group, ISO. */
	at: string;
	parts: Part[];
	href: string;
	count: number;
}

/** A journal row in words, as the server sends it (dashboard/wording.py). */
export interface JournalWords {
	/** Who did it: "HomePilot", a token's label, never an internal id (absent from an older server). */
	actor?: string;
	verb: string;
	/** The thing acted on - a change id or a host name - shown strong; null for none. */
	object: string | null;
	tail: string;
	/** Several in a row by the same actor ("withdrew {n} approved changes"); null: never grouped. */
	many: string | null;
	text: string;
}

/** A journal row as /audit returns it: the row and its words. */
export type WordedEntry = AuditEntry & { words?: JournalWords };

const w = (text: string): Part => ({ text });

/** Words for a row the server sent none for (an older server): never the code. */
const NO_WORDS: JournalWords = { verb: 'recorded an event', object: null, tail: '', many: null, text: 'recorded an event' };

// An internal id (a users/api_tokens uuid) - never said as a name (3.14 walk).
const INTERNAL_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The name a journal actor is said by: "You" for the viewer's own rows, else
 * the server's `actor` words (a token's label, never an internal id). An older
 * server's row without them: its label without the token prefix, and an
 * internal id as a token that no longer exists.
 */
export function actorName(
	userId: string,
	me: { token_label?: string; prefix?: string } | null,
	said?: string,
): string {
	const id = userId || 'system';
	if (me?.token_label) {
		const full = me.prefix ? `${me.token_label} (${me.prefix})` : me.token_label;
		if (id === full || id === me.token_label) return 'You';
	}
	if (said) return said;
	if (id === 'system') return 'HomePilot';
	const label = id.replace(/\s*\(hp_[A-Za-z0-9]+\)\s*$/, '') || id;
	return INTERNAL_ID.test(label) ? 'A token that no longer exists' : label;
}

function hrefOf(e: AuditEntry, base: string): string {
	return e.artifact_id ? `${base}/changes/${e.artifact_id}` : `${base}/records/journal`;
}

/**
 * Journal rows (newest first) as sentences, newest first. Consecutive rows by
 * the same actor with the same known action become one sentence with a count.
 */
export function describeJournal(
	entries: WordedEntry[],
	me: { token_label?: string; prefix?: string } | null,
	base = '',
): Happening[] {
	const out: Happening[] = [];
	let i = 0;
	while (i < entries.length) {
		const e = entries[i];
		const words = e.words ?? NO_WORDS;
		let j = i + 1;
		if (words.many) {
			while (j < entries.length && entries[j].action === e.action && entries[j].user_id === e.user_id) j++;
		}
		const n = j - i;
		const who = actorName(e.user_id, me, e.words?.actor);
		const parts: Part[] =
			n > 1 && words.many
				? [w(`${who} ${words.many.replace('{n}', String(n))}`)]
				: [
						w(`${who} ${words.verb}`),
						...(words.object ? [w(' '), { text: words.object, strong: true }] : []),
						...(words.tail ? [w(words.tail)] : []),
					];
		out.push({ key: `audit:${e.id}`, at: e.timestamp, parts, href: hrefOf(e, base), count: n });
		i = j;
	}
	return out;
}

/** The sentence as plain text (for titles, tests and screen readers). */
export function sentence(h: Happening): string {
	return h.parts.map((p) => p.text).join('');
}

/** A Needs-you run line's words, as the server sends them (dashboard/wording.py). */
export interface RunWords {
	/** "Installing the agent on web-01" - what the run was doing, to what. */
	what: string;
	/** "waiting to start" - how far it got, for "... is still <status>". */
	status: string;
}

/** The words of a run line; an older server's line without them is still not a code. */
export function runWords(r: object): RunWords {
	return (r as { words?: RunWords }).words ?? { what: 'A run', status: 'not finished' };
}
