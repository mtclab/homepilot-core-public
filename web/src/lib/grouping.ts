// Grouped chronology — the pure half of Records (#549 F5).
//
// The 3.4 Records surface was three unbounded newest-first lists: a wall of
// rows with no shape, which is exactly the "long lists of things" the owner
// named. F5's answer is grouping, and the grouping RULES live here rather than
// inside three components so they can be asserted without mounting anything,
// and so tasks and the journal cannot drift into two different ideas of what
// "Today" means.
//
// Everything here is local-time: an operator reading "Today" means their day,
// not UTC's.

/** One day's worth of rows, newest day first. */
export interface DayGroup<T> {
	/** `YYYY-MM-DD` in local time, or `''` for rows with no usable timestamp. */
	key: string;
	/** What the group header says: `Today`, `Yesterday`, or the date. */
	label: string;
	items: T[];
}

function pad(n: number): string {
	return n < 10 ? `0${n}` : String(n);
}

/**
 * The local calendar day an ISO timestamp falls on, as `YYYY-MM-DD`.
 * Returns `''` when the value is missing or unparsable — a row with a broken
 * timestamp must still be shown, not silently dropped.
 */
export function dayKey(ts: string | null | undefined): string {
	if (ts === null || ts === undefined || ts === '') return '';
	const d = new Date(ts);
	if (Number.isNaN(d.getTime())) return '';
	return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** The local calendar day of a Date. */
function keyOfDate(d: Date): string {
	return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * The header for a day key. `Today` / `Yesterday` are relative to `now`, so the
 * page relabels itself over midnight without a reload; everything older is the
 * plain date, spelled out (no bare `03/04` ambiguity).
 */
export function dayLabel(key: string, now: Date = new Date()): string {
	if (!key) return 'Undated';
	const today = keyOfDate(now);
	if (key === today) return 'Today';
	const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
	if (key === keyOfDate(yesterday)) return 'Yesterday';
	const [y, m, d] = key.split('-').map(Number);
	const date = new Date(y, m - 1, d);
	if (Number.isNaN(date.getTime())) return key;
	try {
		return date.toLocaleDateString(undefined, {
			weekday: 'short',
			day: 'numeric',
			month: 'short',
			year: date.getFullYear() === now.getFullYear() ? undefined : 'numeric',
		});
	} catch {
		return key;
	}
}

/**
 * Group rows by their local calendar day, newest day first, preserving the
 * server's order inside each day (the APIs already return newest-first).
 * Undated rows land in one trailing group rather than being hidden.
 */
export function groupByDay<T>(
	items: readonly T[],
	timestampOf: (item: T) => string | null | undefined,
	now: Date = new Date(),
): DayGroup<T>[] {
	const byKey = new Map<string, T[]>();
	for (const item of items) {
		const key = dayKey(timestampOf(item));
		const bucket = byKey.get(key);
		if (bucket) bucket.push(item);
		else byKey.set(key, [item]);
	}
	return [...byKey.keys()]
		// '' sorts before every real key, so undated must be pushed to the end
		// explicitly rather than riding the descending sort.
		.sort((a, b) => {
			if (a === '') return 1;
			if (b === '') return -1;
			return b.localeCompare(a);
		})
		.map((key) => ({ key, label: dayLabel(key, now), items: byKey.get(key) as T[] }));
}

/** Rows matching the predicate, then the rest — both keeping their input order. */
export function partition<T>(
	items: readonly T[],
	predicate: (item: T) => boolean,
): [T[], T[]] {
	const yes: T[] = [];
	const no: T[] = [];
	for (const item of items) (predicate(item) ? yes : no).push(item);
	return [yes, no];
}

/** Rows sharing one kind. */
export interface KindGroup<T> {
	kind: string;
	items: T[];
}

/**
 * Group rows by a kind read OFF THE DATA. The set of kinds is whatever the
 * payload carries — hardcoding a list would silently drop a kind the backend
 * grows later (and the KB's kinds are open: `note`, `policy`, `fact`, `doc`
 * today, and the ingest path can write others).
 *
 * `preferred` only decides ORDER: the kinds an operator reads first go first,
 * anything else follows alphabetically. A kind that is preferred but absent
 * from the data produces no empty group.
 */
export function groupByKind<T>(
	items: readonly T[],
	kindOf: (item: T) => string,
	preferred: readonly string[] = [],
): KindGroup<T>[] {
	const byKind = new Map<string, T[]>();
	for (const item of items) {
		const kind = kindOf(item) || 'other';
		const bucket = byKind.get(kind);
		if (bucket) bucket.push(item);
		else byKind.set(kind, [item]);
	}
	const rank = (k: string): number => {
		const i = preferred.indexOf(k);
		return i === -1 ? preferred.length : i;
	};
	return [...byKind.keys()]
		.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
		.map((kind) => ({ kind, items: byKind.get(kind) as T[] }));
}
