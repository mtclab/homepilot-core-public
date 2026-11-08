<script context="module" lang="ts">
	export type LampState = 'ok' | 'warn' | 'crit' | 'accent' | 'off' | 'half';
</script>

<script lang="ts">
	// A status lamp (direction "Instrument"): filled = a state, hollow = no
	// data, half = parked. A lamp is never the only carrier of status - pass
	// `label` and the word renders beside it; without one the lamp is
	// decorative and the caller must already show the word.
	export let state: LampState = 'off';
	/** The word that carries the same meaning, shown next to the lamp. */
	export let label = '';

	const CLASS: Record<LampState, string> = {
		ok: 'lamp-ok',
		warn: 'lamp-warn',
		crit: 'lamp-crit',
		accent: 'lamp-accent',
		off: '',
		half: 'lamp-half',
	};
</script>

{#if label}
	<span class="inline-flex items-center gap-1.5 {$$props.class ?? ''}" data-lamp={state}>
		<span class="lamp {CLASS[state]}" aria-hidden="true"></span>
		<span>{label}</span>
	</span>
{:else}
	<span class="lamp {CLASS[state]} {$$props.class ?? ''}" data-lamp={state} aria-hidden="true"></span>
{/if}
