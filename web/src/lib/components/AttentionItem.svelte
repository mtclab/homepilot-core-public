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
	/**
	 * Where the fix is OUTSIDE this console (a release's notes): opened in a new
	 * tab, so the operator does not lose the page they were reading.
	 */
	export let external = false;
	/**
	 * What the ONE subject of this line asks of the operator, said under it -
	 * a release's operator actions. Not a way to enumerate several subjects:
	 * those are several lines.
	 */
	export let details: string[] = [];
	/**
	 * When the condition began (ISO). The visible text stays `meta` ("for 3d",
	 * "2h ago"); this makes it a real <time> with the absolute moment on hover,
	 * so "how long" and "since when" are both one glance away (UI plan 4.6).
	 */
	export let since = '';
	/** Stable id of the line, for a page that addresses one (`?focus=` later). */
	export let id = '';

	const chip: Record<string, string> = {
		critical: 'badge-critical',
		warning: 'badge-warning',
		notice: 'badge-notice',
	};
</script>

<!-- The door (the whole sentence) first, then the line's actions: on a wide
     screen they sit at the right end of the line, on a phone under it
     (UI plan 5.2: door link first, then actions). -->
<div class="attention-line" data-attention={id || undefined}>
<svelte:element
	this={href ? 'a' : 'div'}
	href={href || undefined}
	target={href && external ? '_blank' : undefined}
	rel={href && external ? 'noopener noreferrer' : undefined}
	class="attention-door block py-s-1 px-s-2 rounded {href ? 'hover:bg-raised transition-colors' : ''}"
>
	<span class="flex items-baseline gap-s-2">
		<span class="badge {chip[severity]} shrink-0">{label}</span>
		<span class="attention-text prose-body text-sm min-w-0 flex-1" title={text}>{text}</span>
		{#if meta}
			{#if since}
				<time class="text-xs text-muted shrink-0 num-inline" datetime={since} title={new Date(since).toLocaleString()}
					>{meta}</time
				>
			{:else}
				<span class="text-xs text-muted shrink-0 num-inline">{meta}</span>
			{/if}
		{/if}
	</span>
	{#if details.length}
		<ul class="mt-s-1 pl-s-4 list-disc text-xs text-muted prose-body">
			{#each details as detail}<li>{detail}</li>{/each}
		</ul>
	{/if}
</svelte:element>
{#if $$slots.actions}
	<div class="attention-actions"><slot name="actions" /></div>
{/if}
</div>

<style>
	.attention-line {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.attention-door {
		min-width: 0;
		flex: 1;
	}
	/* One line on a wide screen; on a phone the sentence wraps - truncating
	   it there would hide the very words the line exists to say. */
	.attention-text {
		overflow-wrap: anywhere;
	}
	.attention-actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-1);
		padding: 0 var(--space-2) var(--space-1) var(--space-2);
	}
	.attention-actions:empty {
		display: none;
	}
	@media (min-width: 768px) {
		.attention-line {
			flex-direction: row;
			align-items: baseline;
			gap: var(--space-2);
		}
		.attention-text {
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}
		.attention-actions {
			flex-wrap: nowrap;
			flex-shrink: 0;
			padding: 0 var(--space-2) 0 0;
		}
	}
</style>
