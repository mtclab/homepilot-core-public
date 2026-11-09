<script lang="ts">
	// A change in plain words at the top, the raw spec behind a disclosure
	// (owner feedback 2026-11-08). The words come from the server
	// (artifacts/summary.py) so the web, the CLI and the assistant say the
	// same thing; a step it does not recognise is said as the call it is.
	import type { ArtifactSummary } from '$lib/api';
	import Icon from '$lib/components/Icon.svelte';

	export let summary: ArtifactSummary | undefined;
	/** The artifact's text, shown only on request. */
	export let body = '';
	export let rawOpen = false;
</script>

<div class="space-y-2" data-testid="change-summary">
	{#if summary}
		<p class="text-xs text-muted">On <span class="text-ink">{summary.target}</span></p>
		{#if summary.steps.length}
			<ol class="text-sm text-ink space-y-0.5 list-decimal pl-5" data-testid="summary-steps">
				{#each summary.steps as step}
					<li>{step}</li>
				{/each}
			</ol>
		{/if}
		{#if summary.danger === 'destroy'}
			<p class="text-xs text-danger">This cannot be undone: the guest and its disks are deleted.</p>
		{:else if summary.danger === 'stop'}
			<p class="text-xs text-warn">A forced stop: anything unsaved in the guest is lost.</p>
		{/if}
		{#if summary.two_step && summary.two_step_reason}
			<p class="text-xs text-muted">{summary.two_step_reason}</p>
		{/if}
	{/if}
	{#if body}
		<button
			class="text-xs text-accent hover:text-accent-strong"
			aria-expanded={rawOpen}
			on:click={() => (rawOpen = !rawOpen)}
		>
			<Icon name={rawOpen ? 'chevron-down' : 'chevron-right'} /> Raw spec
		</button>
		{#if rawOpen}
			<pre class="code-block text-xs overflow-x-auto whitespace-pre-wrap max-h-96">{body}</pre>
		{/if}
	{/if}
</div>
