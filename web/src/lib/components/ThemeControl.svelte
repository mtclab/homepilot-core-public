<script lang="ts">
	// System / Night / Day (owner decision 2026-11-08). A radio group, so the
	// choice is one keystroke (arrows) and a screen reader hears all three and
	// which is on. Persisted per browser; System follows the OS live.
	import { onMount, onDestroy } from 'svelte';
	import { THEME_CHOICES, readPref, setPref, applyTheme, watchSystemTheme, type ThemePref } from '$lib/theme';

	let pref: ThemePref = 'night';
	let stop: () => void = () => {};

	onMount(() => {
		pref = readPref();
		applyTheme(pref);
		stop = watchSystemTheme();
	});
	onDestroy(() => stop());

	function choose(next: ThemePref) {
		pref = next;
		setPref(next);
	}
</script>

<fieldset class="theme-control">
	<legend class="sr-only">Theme</legend>
	<span class="text-[11px] text-muted" aria-hidden="true">Theme</span>
	{#each THEME_CHOICES as c (c.id)}
		<label class="theme-choice {pref === c.id ? 'is-on' : ''}">
			<input
				class="sr-only"
				type="radio"
				name="hp-theme"
				value={c.id}
				checked={pref === c.id}
				on:change={() => choose(c.id)}
			/>
			{c.label}
		</label>
	{/each}
</fieldset>

<style>
	.theme-control {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.25rem 0.5rem;
		border: 0;
		padding: 0;
		margin: 0;
		font-size: 0.6875rem;
	}
	.theme-choice {
		cursor: pointer;
		color: var(--color-muted);
		border-bottom: 1px solid transparent;
	}
	.theme-choice:hover {
		color: var(--color-text);
	}
	.theme-choice.is-on {
		color: var(--color-text-strong);
		border-bottom-color: var(--color-accent);
	}
	/* The radio itself is visually hidden, so its focus ring goes on the word. */
	.theme-choice:focus-within {
		outline: 2px solid var(--color-accent);
		outline-offset: 2px;
	}
</style>
