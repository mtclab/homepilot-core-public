<script lang="ts">
	// The Alerts list (#660, review 03 step 1): firing now, fired-and-resolved
	// recently, and where alerts are sent. `alert_state` only ever knew the
	// present, so an alert that fired at 02:14 and cleared at 02:21 left no trace
	// by morning; this reads the append-only history instead.
	//
	// Self-contained on purpose: the Overview and the host page each drop it in
	// with one line, and it owns its own loading and empty states.
	import { onMount, onDestroy } from 'svelte';
	import { api, type AlertEvent, type ChannelSummary, type FiringAlert } from '$lib/api';
	import { timeAgo } from '$lib/relativeTime';
	import {
		deliveryText,
		destination,
		firingText,
		resolvedEpisodes,
		resolvedText,
	} from '$lib/alertHistory';

	/** Only this host's alerts (the host page). Empty = the whole estate. */
	export let hostname = '';
	/** Where a hostname links to; empty string = plain text. */
	export let hostHref: (hostname: string) => string = () => '';

	const RANGES = [
		{ hours: 24, label: '24 h' },
		{ hours: 168, label: '7 d' },
	];
	let hours = 24;
	let firing: FiringAlert[] = [];
	let resolved: AlertEvent[] = [];
	let channels: ChannelSummary[] = [];
	let loaded = false;
	let unread = '';
	let timer: ReturnType<typeof setInterval> | undefined;

	async function load() {
		const [now, history] = await Promise.allSettled([
			api.listFiringAlerts(),
			api.listAlertEvents(hours, hostname || undefined),
		]);
		const problems: string[] = [];
		if (now.status === 'fulfilled') {
			firing = (now.value.items ?? []).filter((a) => !hostname || a.hostname === hostname);
		} else problems.push('what is firing');
		if (history.status === 'fulfilled') {
			resolved = resolvedEpisodes(history.value.items ?? []);
			channels = history.value.channels ?? [];
		} else problems.push('the alert history');
		// Never "nothing fired" when the answer is "could not ask".
		unread = problems.length ? `Could not read ${problems.join(' or ')}.` : '';
		loaded = true;
	}

	function setRange(h: number) {
		hours = h;
		void load();
	}

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), 60_000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	$: dest = destination(channels);
	$: rangeLabel = RANGES.find((r) => r.hours === hours)?.label ?? `${hours} h`;
</script>

<section class="card section-stack" aria-labelledby="alerts-{hostname || 'estate'}" data-alerts-panel>
	<div class="flex items-center justify-between gap-2">
		<h2 class="section-title" id="alerts-{hostname || 'estate'}">Alerts</h2>
		<div class="flex gap-1">
			{#each RANGES as r (r.hours)}
				<button
					class="btn text-xs {hours === r.hours ? 'btn-primary' : 'btn-ghost'}"
					aria-pressed={hours === r.hours}
					on:click={() => setRange(r.hours)}>{r.label}</button
				>
			{/each}
		</div>
	</div>

	{#if !hostname && loaded}
		<p
			class="prose-note text-xs {dest.tone === 'danger' ? 'text-danger' : dest.tone === 'warn' ? 'text-warn' : ''}"
			data-alert-destination
		>
			{dest.text}
		</p>
	{/if}

	{#if unread}
		<p class="text-xs text-warn" role="alert">{unread}</p>
	{/if}

	{#if loaded}
		<div>
			<h3 class="text-xs text-muted mb-1">Firing now ({firing.length})</h3>
			{#if firing.length}
				<ul class="flex flex-col" data-alerts-firing>
					{#each firing as a (`${a.rule_id}:${a.hostname}`)}
						<li class="flex items-baseline gap-s-2 py-s-1 px-s-2">
							<span class="badge badge-critical shrink-0">firing</span>
							{#if !hostname}
								{#if hostHref(a.hostname)}
									<a class="font-mono text-sm text-accent shrink-0" href={hostHref(a.hostname)}>{a.hostname}</a>
								{:else}
									<span class="font-mono text-sm text-ink shrink-0">{a.hostname}</span>
								{/if}
							{/if}
							<span class="prose-body text-sm min-w-0 flex-1 truncate" title={firingText(a)}>{firingText(a)}</span>
							<span class="text-xs text-muted shrink-0 num-inline" title={a.firing_since}>since {timeAgo(a.firing_since)}</span>
						</li>
					{/each}
				</ul>
			{:else if !unread}
				<p class="prose-note text-xs">Nothing is firing.</p>
			{/if}
		</div>

		<div>
			<h3 class="text-xs text-muted mb-1">Resolved in the last {rangeLabel} ({resolved.length})</h3>
			{#if resolved.length}
				<ul class="flex flex-col" data-alerts-resolved>
					{#each resolved as e (e.id)}
						<li class="flex items-baseline gap-s-2 py-s-1 px-s-2">
							<span class="badge badge-notice shrink-0">resolved</span>
							{#if !hostname}
								{#if hostHref(e.hostname)}
									<a class="font-mono text-sm text-accent shrink-0" href={hostHref(e.hostname)}>{e.hostname}</a>
								{:else}
									<span class="font-mono text-sm text-ink shrink-0">{e.hostname}</span>
								{/if}
							{/if}
							<span class="prose-body text-sm min-w-0 flex-1 truncate" title={resolvedText(e)}>{resolvedText(e)}</span>
							{#if deliveryText(e)}
								<span class="text-xs shrink-0 {e.delivered_to?.length ? 'text-muted' : 'text-warn'}">{deliveryText(e)}</span>
							{/if}
							<span class="text-xs text-muted shrink-0 num-inline" title={e.at}>{timeAgo(e.at)}</span>
						</li>
					{/each}
				</ul>
			{:else if !unread}
				<p class="prose-note text-xs">
					Nothing fired and resolved in the last {rangeLabel}.
				</p>
			{/if}
		</div>
	{:else}
		<p class="text-muted text-xs">Loading…</p>
	{/if}
</section>
