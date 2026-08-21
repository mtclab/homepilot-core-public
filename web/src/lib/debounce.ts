// Trailing-edge debounce for SSE-driven refetches.
//
// The event bus fires one frame per artifact transition, and a page that
// refetches on every frame turns a 3-artifact apply into 3 full-table reloads
// (Drift issues three parallel calls per frame). Coalescing on the trailing
// edge means a burst costs exactly one refetch, `waitMs` after the last event.
//
// `cancel()` must be called on component destroy so a pending refetch cannot
// fire against a torn-down page.

export interface Debounced<A extends unknown[]> {
	(...args: A): void;
	cancel(): void;
	/** Runs a pending call immediately (no-op when nothing is pending). */
	flush(): void;
	/** True while a trailing call is scheduled. */
	readonly pending: boolean;
}

export function debounce<A extends unknown[]>(fn: (...args: A) => void, waitMs = 400): Debounced<A> {
	let timer: ReturnType<typeof setTimeout> | null = null;
	let lastArgs: A | null = null;

	const run = () => {
		timer = null;
		const args = lastArgs;
		lastArgs = null;
		if (args) fn(...args);
	};

	const debounced = ((...args: A) => {
		lastArgs = args;
		if (timer) clearTimeout(timer);
		timer = setTimeout(run, waitMs);
	}) as Debounced<A>;

	debounced.cancel = () => {
		if (timer) clearTimeout(timer);
		timer = null;
		lastArgs = null;
	};

	debounced.flush = () => {
		if (!timer) return;
		clearTimeout(timer);
		run();
	};

	Object.defineProperty(debounced, 'pending', { get: () => timer !== null });

	return debounced;
}
