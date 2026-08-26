<script lang="ts">
	// One enumerated drift item (#549 F4).
	//
	// Only artifacts whose reality DISAGREES get one of these — drifted, or a
	// check that established nothing. The two classes render the same card and
	// differ only in what the summary line says, which is why this is one
	// component and not two near-identical blocks in the page.
	import { base } from '$app/paths';
	import type { DriftItem } from '$lib/drift';
	import { timeAgo } from '$lib/relativeTime';

	export let item: DriftItem;
	/** Prefix for the summary line, e.g. "Not established:". */
	export let summaryPrefix = '';
	export let rechecking = false;
	export let onRecheck: (artifactId: string) => void = () => {};

	const STATUS_CLASSES: Record<string, string> = {
		proposed: 'badge-proposed',
		approved: 'badge-approved',
		applied: 'badge-applied',
		rejected: 'badge-rejected',
		revoked: 'badge-revoked',
		failed: 'badge-failed',
		superseded: 'badge-superseded',
	};
	$: statusClass = STATUS_CLASSES[item.artifact.status] ?? 'badge-proposed';
</script>

<article class="card space-y-2" data-testid="drift-item">
	<div class="flex items-start justify-between gap-3">
		<div class="min-w-0 space-y-1">
			<a
				href="{base}/changes/{item.artifact.id}"
				class="text-sm text-ink-strong font-medium hover:text-accent-strong">{item.artifact.intent}</a
			>
			<div class="flex flex-wrap items-center gap-2 text-xs text-muted">
				<span class="badge {statusClass}">{item.artifact.status}</span>
				<span>{item.artifact.kind}</span>
				<span>·</span>
				{#if item.host}
					<a
						href="{base}/inventory/{item.host.id}"
						class="font-mono text-accent hover:text-accent-strong">{item.host.hostname}</a
					>
				{:else}
					<span class="font-mono text-warn">{item.target || '(global)'}</span>
				{/if}
				{#if item.orphan}
					<span class="text-warn">target not in inventory</span>
				{/if}
			</div>
		</div>
		{#if rechecking}
			<span class="text-muted text-xs animate-pulse shrink-0">checking…</span>
		{:else}
			<button class="btn btn-ghost text-xs shrink-0" on:click={() => onRecheck(item.artifact.id)}
				>↻ Recheck</button
			>
		{/if}
	</div>
	<p class="prose-note text-xs" data-testid="drift-item-summary">
		{summaryPrefix ? summaryPrefix + ' ' : ''}{item.summary}
	</p>
	<p class="text-muted text-xs">checked {timeAgo(item.check?.checked_at)}</p>
</article>
