<script context="module" lang="ts">
	export type ReadoutTone = 'neutral' | 'ok' | 'warn' | 'crit';
</script>

<script lang="ts">
	// One gauge in a readout row (direction "Instrument"): a small label, a
	// mono number, an optional unit and one muted line of what it counts. The
	// row is the unit, not the gauge: readouts sit side by side in one panel
	// separated by hairlines - no card per number, no lift on hover.
	//
	// The tone repeats what the number means (the sub line says it in words);
	// it never stands alone.
	export let label: string;
	export let value: string | number;
	/** Small mono text after the value: "/14", "GiB". */
	export let unit = '';
	export let sub = '';
	export let tone: ReadoutTone = 'neutral';
	/** The page that holds the rows behind this number. */
	export let href = '';

	const TONE: Record<ReadoutTone, string> = {
		neutral: '',
		ok: 'text-ok',
		warn: 'text-warn',
		crit: 'text-danger',
	};
</script>

<svelte:element this={href ? 'a' : 'div'} href={href || undefined} class="readout" data-readout={label}>
	<span class="readout-k">{label}</span>
	<span class="readout-v metric {TONE[tone]}">{value}{#if unit}<small>{unit}</small>{/if}</span>
	{#if sub}<span class="readout-sub">{sub}</span>{/if}
</svelte:element>

<style>
	.readout {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
		min-width: 0;
		padding: 0.5rem 0.625rem;
		color: var(--color-text);
		transition: background-color 120ms ease;
	}
	a.readout:hover {
		background: var(--color-surface-raised);
	}
	.readout-k {
		font-size: 0.6875rem;
		font-weight: 700;
		color: var(--color-muted);
	}
	.readout-v {
		font-size: 1.25rem;
		line-height: 1.15;
		color: var(--color-text-strong);
	}
	.readout-v.text-ok {
		color: var(--color-ok);
	}
	.readout-v.text-warn {
		color: var(--color-warn);
	}
	.readout-v.text-danger {
		color: var(--color-danger);
	}
	.readout-v small {
		font-size: 0.6875rem;
		font-weight: 400;
		margin-left: 0.1875rem;
		letter-spacing: 0;
		color: var(--color-muted);
	}
	.readout-sub {
		font-family: var(--font-serif);
		font-size: 0.71875rem;
		line-height: 1.4;
		color: var(--color-muted);
	}
</style>
