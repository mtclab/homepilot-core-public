<script lang="ts">
	// A panel: the instrument's unit. A head strip (title left, mono meta
	// right), a hairline, then the body. `id` names the region for a page
	// that needs to address it; the title labels it for assistive tech.
	export let title = '';
	/** Short mono line on the right of the head: a count, a time, a source. */
	export let meta = '';
	export let id: string | undefined = undefined;
	/** Heading level of the title, so a page keeps a sane outline. */
	export let level: 2 | 3 = 2;
	/** Drop the body padding for content that brings its own (a table). */
	export let flush = false;

	const uid = `panel-${Math.random().toString(36).slice(2, 9)}`;
	$: headingId = id ? `${id}-title` : uid;
</script>

<section class="panel {$$props.class ?? ''}" {id} aria-labelledby={title ? headingId : undefined}>
	{#if title || meta || $$slots.actions}
		<div class="panel-head">
			{#if title}
				<svelte:element this={`h${level}`} class="panel-title" id={headingId}>{title}</svelte:element>
			{/if}
			<div class="flex items-baseline gap-s-2">
				{#if meta}<span class="panel-meta">{meta}</span>{/if}
				<slot name="actions" />
			</div>
		</div>
	{/if}
	<div class={flush ? '' : 'panel-body'}>
		<slot />
	</div>
</section>
