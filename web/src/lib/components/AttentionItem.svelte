<script lang="ts">
	// One line in a "needs attention" list (#549 F1, principle 1).
	//
	// The shape every attention zone shares: how urgent, what happened, and a
	// door straight to the surface where it is fixed. Built here in F1 so the
	// Overview zone (F2) and the Host page's per-host attention list (F3) —
	// developed in parallel — cannot each invent their own.
	//
	// It never enumerates: one item is one line. Detail lives behind the link.

	/**
	 * How urgent, NOT what state the thing is in. Lifecycle badges
	 * (proposed/applied/rejected) name a state; these name urgency, which is why
	 * they are a separate vocabulary rather than a reuse of those.
	 */
	export let severity: 'critical' | 'warning' | 'notice' = 'notice';
	/** The chip's text, e.g. "drifted", "failed", "offline". */
	export let label: string;
	/** The one-line description an operator reads. */
	export let text: string;
	/** Where the fix is. Empty renders the item as plain text, not a dead link. */
	export let href = '';
	/** Optional trailing context: a timestamp, a count. Never the fix itself. */
	export let meta = '';

	const chip: Record<string, string> = {
		critical: 'badge-critical',
		warning: 'badge-warning',
		notice: 'badge-notice',
	};
</script>

<svelte:element
	this={href ? 'a' : 'div'}
	href={href || undefined}
	class="flex items-baseline gap-s-2 py-s-1 px-s-2 rounded {href
		? 'hover:bg-raised transition-colors'
		: ''}"
>
	<span class="badge {chip[severity]} shrink-0">{label}</span>
	<span class="prose-body text-sm min-w-0 flex-1 truncate" title={text}>{text}</span>
	{#if meta}<span class="text-xs text-muted shrink-0 num-inline">{meta}</span>{/if}
</svelte:element>
