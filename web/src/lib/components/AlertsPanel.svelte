<script lang="ts">
	// The Alerts list (#660, review 03 step 1): firing now, fired-and-resolved
	// recently, and where alerts are sent. `alert_state` only ever knew the
	// present, so an alert that fired at 02:14 and cleared at 02:21 left no trace
	// by morning; this reads the append-only history instead.
	//
	// Self-contained on purpose: the Overview and the host page each drop it in
	// with one line, and it owns its own loading and empty states.
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { api, sessionStore, type AlertEvent, type ChannelSummary, type FiringAlert } from '$lib/api';
	import { isAdmin as capIsAdmin } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { timeAgo } from '$lib/relativeTime';
	import {
		deliveryRows,
		deliveryText,
		destination,
		firingText,
		resolvedEpisodes,
		resolvedText,
	} from '$lib/alertHistory';
	import Lamp from './Lamp.svelte';

	/** Only this host's alerts (the host page). Empty = the whole estate. */
	export let hostname = '';
	/** Where a hostname links to; empty string = plain text. */
	export let hostHref: (hostname: string) => string = () => '';
	/**
	 * Monitoring > Alerts (UI slice 3): the panel IS the page there, so it also
	 * carries each firing alert's rule, the deliveries list and "Send test".
	 * The host page drops it in without these.
	 */
	export let page = false;

	const RANGES = [
		{ hours: 24, label: '24 h' },
		{ hours: 168, label: '7 d' },
	];
	let hours = 24;
	let firing: FiringAlert[] = [];
	let resolved: AlertEvent[] = [];
	let channels: ChannelSummary[] = [];
	let events: AlertEvent[] = [];
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
			events = history.value.items ?? [];
			resolved = resolvedEpisodes(events);
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

	// "Send test" (plan 3.5): proves the path to the phone from where the
	// operator is reading alerts. Channels are admin plumbing (their URLs are
	// credentials), so a non-admin session is told why there is no button.
	$: admin = capIsAdmin($sessionStore?.capabilities);
	$: enabled = channels.filter((c) => c.enabled);
	let testing = false;
	let testResult = '';
	let testOk = true;

	async function sendTest() {
		testing = true;
		testResult = '';
		const outcomes: string[] = [];
		let allOk = true;
		for (const c of enabled) {
			try {
				const res = await api.testNotificationChannel(c.id);
				if (res.delivered) outcomes.push(`${c.name}: accepted`);
				else {
					allOk = false;
					outcomes.push(`${c.name}: NOT accepted (${res.error ?? 'no reason given'})`);
				}
			} catch (e) {
				allOk = false;
				outcomes.push(`${c.name}: could not send (${e instanceof Error ? e.message : String(e)})`);
			}
		}
		testOk = allOk;
		testResult = `Test message - ${outcomes.join('; ')}.`;
		notify(testResult, allOk ? 'ok' : 'err');
		testing = false;
		// The channel's state line changes with the test; read it again.
		await load();
	}

	$: deliveries = page ? deliveryRows(events) : [];
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
		<div class="flex flex-wrap items-baseline gap-x-s-3 gap-y-s-1">
			<p
				class="prose-note text-xs {dest.tone === 'danger' ? 'text-danger' : dest.tone === 'warn' ? 'text-warn' : ''}"
				data-alert-destination
			>
				{dest.text}
			</p>
			{#if page}
				{#if !admin}
					<span class="text-xs text-muted">Sending a test needs an admin session.</span>
				{:else if enabled.length}
					<button class="btn btn-ghost btn-sm" on:click={sendTest} disabled={testing} data-send-test>
						{testing ? 'Sending...' : 'Send test'}
					</button>
				{:else}
					<a class="text-accent text-xs" href="{base}/monitoring/rules#notification-channels">Add a channel</a>
				{/if}
			{/if}
		</div>
		{#if page && testResult}
			<p class="text-xs {testOk ? 'text-ok' : 'text-danger'}" data-send-test-result>{testResult}</p>
		{/if}
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
						<li class="alert-row py-s-1 px-s-2">
							<span class="badge badge-critical shrink-0">firing</span>
							{#if !hostname}
								{#if hostHref(a.hostname)}
									<a class="font-mono text-sm text-accent shrink-0" href={hostHref(a.hostname)}>{a.hostname}</a>
								{:else}
									<span class="font-mono text-sm text-ink shrink-0">{a.hostname}</span>
								{/if}
							{/if}
							<span class="alert-row-text prose-body text-sm" title={firingText(a)}>{firingText(a)}</span>
							{#if page}
								<a class="text-accent text-xs shrink-0" href="{base}/monitoring/rules#rule-{a.rule_id}" data-rule-link
									>rule</a
								>
							{/if}
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
						<li class="alert-row py-s-1 px-s-2">
							<span class="badge badge-notice shrink-0">resolved</span>
							{#if !hostname}
								{#if hostHref(e.hostname)}
									<a class="font-mono text-sm text-accent shrink-0" href={hostHref(e.hostname)}>{e.hostname}</a>
								{:else}
									<span class="font-mono text-sm text-ink shrink-0">{e.hostname}</span>
								{/if}
							{/if}
							<span class="alert-row-text prose-body text-sm" title={resolvedText(e)}>{resolvedText(e)}</span>
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

		{#if page}
			<!-- Did it reach anyone? One row per transition whose delivery is known:
			     an alert nobody received is not an alert (plan 3.5). -->
			<div>
				<h3 class="text-xs text-muted mb-1">Deliveries in the last {rangeLabel} ({deliveries.length})</h3>
				{#if deliveries.length}
					<ul class="flex flex-col" data-alerts-deliveries>
						{#each deliveries as r (r.id)}
							<li class="alert-row py-s-1 px-s-2">
								<Lamp state={r.reached ? 'ok' : 'warn'} />
								<span class="text-xs text-muted shrink-0 num-inline w-16" title={r.at}>{timeAgo(r.at)}</span>
								{#if hostHref(r.hostname)}
									<a class="font-mono text-sm text-accent shrink-0" href={hostHref(r.hostname)}>{r.hostname}</a>
								{:else}
									<span class="font-mono text-sm text-ink shrink-0">{r.hostname}</span>
								{/if}
								<span class="alert-row-text prose-body text-sm" title={r.what}>{r.what}</span>
								<span class="text-xs shrink-0 {r.reached ? 'text-muted' : 'text-warn'}">{r.outcome}</span>
							</li>
						{/each}
					</ul>
				{:else if !unread}
					<p class="prose-note text-xs">No alert was sent anywhere in the last {rangeLabel}.</p>
				{/if}
			</div>
		{/if}
	{:else}
		<p class="text-muted text-xs">Loading…</p>
	{/if}
</section>

<style>
	/* One line per alert on a desk. On a phone the sentence takes its own
	   line under the host and the times - truncated to a few letters it said
	   nothing. */
	.alert-row {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		column-gap: var(--space-2);
	}
	.alert-row-text {
		order: 9;
		flex: 1 0 100%;
		min-width: 0;
	}
	@media (min-width: 768px) {
		.alert-row {
			flex-wrap: nowrap;
		}
		.alert-row-text {
			order: 0;
			flex: 1 1 0%;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}
	}
</style>
