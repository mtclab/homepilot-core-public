// "last checked 12m ago" — the shape a rollup line needs (#549 F4).
//
// A rolled-up line has room for exactly one time, and an absolute timestamp
// makes the reader do the subtraction. Kept pure and injectable so the
// rendering can be asserted without freezing the clock.

export function timeAgo(iso: string | null | undefined, now: Date = new Date()): string {
	if (!iso) return 'never';
	const then = new Date(iso);
	const ms = then.getTime();
	if (Number.isNaN(ms)) return 'never';
	const secs = Math.round((now.getTime() - ms) / 1000);
	// A clock skew between the browser and the server must not read as the
	// future; "just now" is the honest answer for anything under a minute.
	if (secs < 60) return 'just now';
	const mins = Math.floor(secs / 60);
	if (mins < 60) return `${mins}m ago`;
	const hours = Math.floor(mins / 60);
	if (hours < 24) return `${hours}h ago`;
	const days = Math.floor(hours / 24);
	if (days < 30) return `${days}d ago`;
	return then.toLocaleDateString();
}

// "redeemed 2 months ago", "expires in 5 days" (3.12 Guests): a date in words,
// in either direction, for a sentence that has room for one. The exact time
// belongs in a hover (`exactTime`), never in the sentence itself.
const WORD_STEPS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
	['second', 60],
	['minute', 60],
	['hour', 24],
	['day', 30],
	['month', 12],
	['year', Number.POSITIVE_INFINITY],
];

export function inWords(iso: string | null | undefined, now: Date = new Date()): string {
	if (!iso) return 'never';
	const ms = new Date(iso).getTime();
	if (Number.isNaN(ms)) return 'never';
	let value = (ms - now.getTime()) / 1000;
	if (Math.abs(value) < 45) return 'just now';
	const fmt = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
	for (const [unit, size] of WORD_STEPS) {
		if (Math.abs(value) < size) return fmt.format(Math.round(value), unit);
		value /= size;
	}
	return fmt.format(Math.round(value), 'year');
}

/** The exact moment, for a hover: the reader's own locale and time zone. */
export function exactTime(iso: string | null | undefined): string {
	if (!iso) return '';
	const d = new Date(iso);
	return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}
