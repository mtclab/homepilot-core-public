<script lang="ts">
	// Operator power controls for one guest (lane B3, #781). Self-contained like
	// HostNetwork: the host page drops it in with one line and it owns its
	// loading, its unread state, its confirmations and its result.
	//
	// What it shows is the server's answer, never a guess: the live state from
	// Proxmox, the pending changes a Reboot would apply, which actions are
	// possible now, and after an action its OUTCOME - ok, failed, timed out or
	// unknown. A shutdown the guest ignored says so and offers Stop; it is never
	// shown as done.
	import { onMount } from 'svelte';
	import Icon from '$lib/components/Icon.svelte';
	import {
		api,
		type GuestPowerResult,
		type GuestPowerState,
		type PowerAction,
	} from '$lib/api';

	export let hostId: string;

	const ORDER: PowerAction[] = ['start', 'shutdown', 'reboot', 'stop'];
	const LABELS: Record<PowerAction, string> = {
		start: 'Start',
		shutdown: 'Shut down',
		reboot: 'Reboot',
		stop: 'Stop (forced)',
	};
	const EXPLAIN: Record<PowerAction, string> = {
		start: 'Starts the guest. Refused if a running guest holds one of its addresses or its passthrough disk.',
		shutdown:
			'Asks the guest to shut down (ACPI / guest agent). If it has not stopped within two minutes it is left running and you are told; nothing is forced.',
		reboot: 'A Proxmox reboot (shut down, then start): the pending changes below are applied.',
		stop: 'Cuts the power, like pulling the plug: anything unsaved in the guest is lost.',
	};
	const OUTCOME: Record<GuestPowerResult['outcome'], string> = {
		ok: 'Done',
		failed: 'Failed',
		timed_out: 'Timed out',
		unknown: 'Outcome unknown',
	};

	let state: GuestPowerState | null = null;
	let unread = '';
	let chosen: PowerAction | null = null;
	let typed = '';
	// Stop (forced) on an ordinary guest asks twice: this is the second ask.
	let stopArmed = false;
	let running = false;
	let result: GuestPowerResult | null = null;
	let refused = '';

	async function load() {
		try {
			state = await api.getGuestPower(hostId);
			unread = '';
		} catch (e) {
			// Never "stopped" when the answer is "could not ask".
			unread = e instanceof Error ? e.message : String(e);
		}
	}

	onMount(() => void load());

	function choose(action: PowerAction) {
		chosen = action;
		typed = '';
		stopArmed = false;
		refused = '';
		result = null;
	}

	function cancel() {
		chosen = null;
		typed = '';
		stopArmed = false;
	}

	$: needsName = !!(chosen && state?.actions[chosen]?.needs_typed_confirmation);
	$: nameOk = !needsName || typed.trim().toLowerCase() === (state?.hostname ?? '').toLowerCase();

	async function run() {
		if (!chosen || !state) return;
		if (chosen === 'stop' && !needsName && !stopArmed) {
			stopArmed = true;
			return;
		}
		running = true;
		refused = '';
		try {
			result = await api.powerGuest(hostId, {
				action: chosen,
				...(needsName ? { confirm_name: typed.trim() } : {}),
			});
			chosen = null;
		} catch (e) {
			refused = e instanceof Error ? e.message : String(e);
		} finally {
			running = false;
			stopArmed = false;
			await load();
		}
	}

	function show(v: unknown): string {
		return v === undefined || v === null ? '—' : String(v);
	}
</script>

<section class="card p-4 space-y-3" aria-labelledby="guest-power" id="power" data-guest-power>
	<h2 class="section-title" id="guest-power">Power</h2>
	{#if unread}
		<p class="text-danger text-sm">Could not read the power state: {unread}</p>
	{:else if !state}
		<p class="prose-note text-sm">Reading the guest's state from Proxmox…</p>
	{:else if state.error}
		<p class="text-danger text-sm">{state.error}</p>
	{:else}
		<p class="text-sm">
			<span class="text-muted">State</span>
			<span class="text-ink" data-power-status>{state.status}</span>
			{#if state.is_self}
				<span class="badge bg-raised text-muted">HomePilot runs here</span>
			{:else if state.protected}
				<span class="badge bg-raised text-muted">protected</span>
			{/if}
		</p>
		{#if state.pending.length}
			<div class="text-sm">
				<p class="text-muted">Pending until the next reboot:</p>
				<ul class="font-mono text-xs" data-power-pending>
					{#each state.pending as p (p.key)}
						<!-- The arrow is an icon (the glyph gate), named so a screen reader still hears the change. -->
						<li>
							{#if p.delete}{p.key}: remove ({show(p.current)}){:else}{p.key}: {show(p.current)}
								<Icon name="arrow-right" size={12} label="becomes" />
								{show(p.pending)}{/if}
						</li>
					{/each}
				</ul>
			</div>
		{/if}
		<div class="flex gap-2 flex-wrap">
			{#each ORDER as action (action)}
				{#if state.actions[action]?.available}
					<button
						type="button"
						class="btn btn-ghost text-xs {action === 'stop' ? 'text-danger' : ''}"
						disabled={running}
						on:click={() => choose(action)}>{LABELS[action]}</button>
				{/if}
			{/each}
		</div>

		{#if chosen}
			<div class="space-y-2 text-sm" data-power-confirm>
				<p class="text-ink">{LABELS[chosen]} <strong>{state.hostname}</strong>?</p>
				<p class="prose-note">{EXPLAIN[chosen]}</p>
				{#if needsName}
					<label class="block">
						<span class="text-muted">
							{state.is_self
								? 'This is the machine HomePilot runs on; HomePilot goes down with it.'
								: 'This guest is protected.'}
							Type its name to confirm:
						</span>
						<input class="input mt-1" bind:value={typed} autocomplete="off" aria-label="Type the guest's name" />
					</label>
				{/if}
				{#if stopArmed}
					<p class="text-danger">Really force it off? Unsaved work in {state.hostname} is lost.</p>
				{/if}
				<div class="flex gap-2">
					<button
						type="button"
						class="btn {chosen === 'stop' ? 'btn-danger' : 'btn-primary'} text-xs"
						disabled={running || !nameOk}
						on:click={run}
					>
						{running ? 'Working…' : stopArmed ? 'Yes, force it off' : LABELS[chosen]}
					</button>
					<button type="button" class="btn btn-ghost text-xs" disabled={running} on:click={cancel}>Cancel</button>
				</div>
			</div>
		{/if}
	{/if}

	{#if refused}
		<p class="text-danger text-sm" role="alert" data-power-refused>{refused}</p>
	{/if}
	{#if result}
		<div class="text-sm" role="status" data-power-result={result.outcome}>
			<p>
				<span class={result.outcome === 'ok' ? 'text-ink' : 'text-danger'}>{OUTCOME[result.outcome]}:</span>
				{result.message}
			</p>
			{#if result.next_actions.length && state}
				<div class="flex gap-2 mt-1">
					{#each result.next_actions as next (next)}
						{#if state.actions[next]?.available}
							<button type="button" class="btn btn-ghost text-xs" on:click={() => choose(next)}>{LABELS[next]}</button>
						{/if}
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</section>
