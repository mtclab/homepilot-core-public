<script lang="ts">
	// A headline metric: big number + label, optional sub and accent color.
	export let label: string;
	export let value: string | number;
	export let sub = '';
	export let accent: 'emerald' | 'sky' | 'yellow' | 'red' | 'slate' = 'slate';
	export let href = '';

	const ring: Record<string, string> = {
		emerald: 'text-emerald-400',
		sky: 'text-sky-400',
		yellow: 'text-yellow-400',
		red: 'text-red-400',
		slate: 'text-slate-200'
	};
</script>

<svelte:element
	this={href ? 'a' : 'div'}
	href={href || undefined}
	class="card flex flex-col gap-1 {href ? 'hover:border-slate-500 transition-colors' : ''}"
>
	<span class="text-xs text-slate-400">{label}</span>
	<span class="text-3xl font-bold font-mono {ring[accent]}">{value}</span>
	{#if sub}<span class="text-xs text-slate-500">{sub}</span>{/if}
</svelte:element>
