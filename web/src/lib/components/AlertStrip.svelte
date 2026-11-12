<script lang="ts">
	// The Overview's alert strip (UI slice 3, plan 3.1): two lines that answer
	// "would I know?" - how many fire, how many fired and cleared today, and
	// where an alert goes - with the door to Monitoring > Alerts. The list
	// itself lives on that page; the Overview never repeats it (plan 2.4).
	//
	// The page hands in what it read, so a read that failed is said as such:
	// `null` is "could not read", never "nothing fired".
	import { base } from '$app/paths';
	import type { AlertHistory, FiringAlert } from '$lib/api';
	import { destination, resolvedEpisodes } from '$lib/alertHistory';
	import Icon from './Icon.svelte';
	import Lamp, { type LampState } from './Lamp.svelte';

	export let firing: FiringAlert[] | null;
	export let history: AlertHistory | null;
	/** Enabled rules: with none, "0 firing" is not calm, it is unwatched. */
	export let rulesEnabled: number;

	$: resolved = history ? resolvedEpisodes(history.items ?? []).length : null;
	$: dest = history ? destination(history.channels ?? []) : null;
	let lamp: LampState = 'off';
	$: lamp = firing === null ? 'warn' : firing.length > 0 ? 'crit' : rulesEnabled > 0 ? 'ok' : 'off';
	$: hours = history?.hours ?? 24;
</script>

<section class="panel alert-strip" aria-labelledby="alert-strip-title" data-alert-strip>
	<div class="panel-head">
		<h2 class="panel-title" id="alert-strip-title">Alerts</h2>
		<a class="text-accent text-xs" href="{base}/monitoring/alerts" data-alert-strip-door
			>Monitoring <Icon name="arrow-right" /></a
		>
	</div>
	<div class="panel-body flex flex-col gap-s-1 text-sm">
		<p class="flex items-baseline gap-s-2" data-alert-strip-count>
			<Lamp state={lamp} />
			{#if firing === null}
				<span class="text-warn">Could not read what is firing.</span>
			{:else}
				<span>
					<b class="metric text-ink-strong">{firing.length}</b> firing
					{#if resolved !== null}
						<span class="text-muted">· {resolved} resolved in the last {hours} h</span>
					{/if}
					{#if firing.length === 0 && rulesEnabled === 0}
						<span class="text-warn">· no rule is enabled</span>
					{/if}
				</span>
			{/if}
		</p>
		<p
			class="prose-note text-xs {dest?.tone === 'danger'
				? 'text-danger'
				: dest?.tone === 'warn' || dest === null
					? 'text-warn'
					: ''}"
			data-alert-strip-destination
		>
			{dest ? dest.text : 'Could not read the alert history, so where alerts go is unknown here.'}
		</p>
	</div>
</section>
