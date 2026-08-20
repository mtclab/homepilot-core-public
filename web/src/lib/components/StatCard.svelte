<script lang="ts">
	// A headline metric: big number + label, optional sub and status tone.
	// The tone names say what the number MEANS, not which swatch it uses — the
	// swatch lives in the token layer (src/app.css).
	export let label: string;
	export let value: string | number;
	export let sub = '';
	export let accent: 'ok' | 'warn' | 'danger' | 'accent' | 'neutral' = 'neutral';
	export let href = '';

	const tone: Record<string, string> = {
		ok: 'text-ok',
		warn: 'text-warn',
		danger: 'text-danger',
		accent: 'text-accent',
		neutral: 'text-ink-strong'
	};
</script>

<svelte:element
	this={href ? 'a' : 'div'}
	href={href || undefined}
	class="card flex flex-col gap-1 {href ? 'hover:border-accent transition-colors' : ''}"
>
	<span class="field-label">{label}</span>
	<span class="metric text-3xl {tone[accent]}">{value}</span>
	{#if sub}<span class="prose-note text-xs">{sub}</span>{/if}
</svelte:element>
