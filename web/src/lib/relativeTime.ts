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
