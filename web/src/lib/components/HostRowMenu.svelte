<script lang="ts">
	// One host's row menu on the fleet list (UI plan 3.2, slice 5): Open ·
	// Metrics · Open in Proxmox · Console · Start / Shut down / Reboot / Stop ·
	// Adopt / Ignore · Forget. The same component serves the table row and the
	// mobile card, so the two can never offer different verbs.
	//
	// The power verbs are judged by the guest's LIVE state, read from Proxmox
	// through the server when the menu opens (`GET /inventory/{id}/power`): a
	// row can be a sync interval old, and a Start offered on a guest that is
	// already running is a refusal waiting to happen. A verb the state does
	// not allow stays in the menu, disabled, with its reason beside it.
	//
	// Confirmation follows plan 5.3: Start / Shut down / Reboot of an ordinary
	// guest turn into "Confirm <verb>" in place (tier 2); a forced Stop, and
	// any verb on the machine HomePilot runs on or a protected guest, ask the
	// page for the typed dialog (tier 3). The page runs the action.
	import { createEventDispatcher } from 'svelte';
	import { base } from '$app/paths';
	import { api, hostMetricsUrl, type GuestPowerState, type Host, type PowerAction } from '$lib/api';
	import { isPowerable, needsTypedName, verbStates } from '$lib/powerMenu';
	import RowMenu from './RowMenu.svelte';
	import Icon from './Icon.svelte';

	export let host: Host;
	export let canWrite = false;
	export let canAdmin = false;
	/** A power action is running on this host: no second one from its menu. */
	export let busy = false;
	/** The page is forgetting this host. */
	export let forgetting = false;

	const dispatch = createEventDispatcher<{
		power: { action: PowerAction; state: GuestPowerState; typed: boolean };
		adopt: void;
		ignore: void;
		forget: void;
	}>();

	let state: GuestPowerState | null = null;
	let reading = false;
	let unread = '';
	let confirming: PowerAction | null = null;
	let forgetConfirm = false;

	$: powerable = isPowerable(host);
	$: verbs = verbStates(state, { canAdmin, reading, unread });

	async function readState() {
		confirming = null;
		forgetConfirm = false;
		if (!powerable || !canAdmin) return;
		reading = true;
		unread = '';
		try {
			const s = await api.getGuestPower(host.id);
			if (!s) throw new Error('no answer');
			state = s;
		} catch (e) {
			// Never "stopped" when the answer is "could not ask".
			state = null;
			unread = e instanceof Error ? e.message : String(e);
		} finally {
			reading = false;
		}
	}

	function choose(action: PowerAction, close: () => void) {
		if (!state || busy) return;
		if (needsTypedName(state, action)) {
			close();
			dispatch('power', { action, state, typed: true });
			return;
		}
		confirming = action;
	}

	function confirmInline(close: () => void) {
		if (!state || !confirming) return;
		const action = confirming;
		confirming = null;
		close();
		dispatch('power', { action, state, typed: false });
	}
</script>

<RowMenu label="Actions for {host.hostname}" let:close on:open={readState}>
	<a role="menuitem" class="btn btn-ghost btn-xs text-left" href="{base}/hosts/{host.id}" on:click={close}
		>Open host</a
	>
	{#if host.hostname}
		<a
			role="menuitem"
			class="btn btn-ghost btn-xs text-left"
			href={hostMetricsUrl(base, host.hostname, host.id)}
			title="Open this host's recent metrics"
			on:click={close}>Metrics</a
		>
	{/if}
	{#if host.proxmox_url}
		<a
			role="menuitem"
			class="btn btn-ghost btn-xs text-left"
			href={host.proxmox_url}
			target="_blank"
			rel="noopener noreferrer"
			title="This host's page in the Proxmox web UI"
			on:click={close}>Open in Proxmox <Icon name="external-link" size={12} /></a
		>
	{/if}
	{#if host.console_url}
		<a
			role="menuitem"
			class="btn btn-ghost btn-xs text-left"
			href={host.console_url}
			target="_blank"
			rel="noopener noreferrer"
			title="The guest's console in the Proxmox web UI (you log in to Proxmox there)"
			on:click={close}><Icon name="terminal" size={12} />Console</a
		>
	{/if}

	{#if powerable}
		<div role="separator" class="menu-sep"></div>
		{#each verbs as v (v.action)}
			{#if confirming === v.action}
				<button
					type="button"
					role="menuitem"
					class="btn btn-primary btn-xs text-left"
					on:click={() => confirmInline(close)}>Confirm {v.label.toLowerCase()}</button
				>
				<button
					type="button"
					role="menuitem"
					class="btn btn-ghost btn-xs text-left"
					on:click={() => (confirming = null)}>Cancel</button
				>
			{:else}
				<button
					type="button"
					role="menuitem"
					class="btn btn-ghost btn-xs text-left menu-verb {v.action === 'stop' && v.enabled
						? 'text-danger'
						: ''}"
					disabled={!v.enabled || busy}
					data-verb={v.action}
					aria-label={!v.enabled
						? `${v.label} (${v.why})`
						: busy
							? `${v.label} (another action is running)`
							: v.label}
					on:click={() => choose(v.action, close)}
					><span>{v.label}</span>{#if !v.enabled}<span class="menu-why">{v.why}</span
						>{:else if busy}<span class="menu-why">Another action is running</span>{/if}</button
				>
			{/if}
		{/each}
	{/if}

	{#if canWrite && host.source === 'discovered' && host.import_state !== 'ignored' && !host.is_template}
		<div role="separator" class="menu-sep"></div>
		<button
			type="button"
			role="menuitem"
			class="btn btn-ghost btn-xs text-left"
			on:click={() => {
				close();
				dispatch('adopt');
			}}>Adopt</button
		>
		<button
			type="button"
			role="menuitem"
			class="btn btn-ghost btn-xs text-left"
			on:click={() => {
				close();
				dispatch('ignore');
			}}>Ignore</button
		>
	{/if}
	{#if canWrite && (host.source === 'manual' || host.absent_since)}
		<div role="separator" class="menu-sep"></div>
		{#if forgetConfirm}
			<button
				type="button"
				role="menuitem"
				class="btn btn-danger btn-xs text-left"
				disabled={forgetting}
				on:click={() => dispatch('forget')}>{forgetting ? 'Removing…' : 'Confirm forget'}</button
			>
			<button
				type="button"
				role="menuitem"
				class="btn btn-ghost btn-xs text-left"
				on:click={() => (forgetConfirm = false)}>Cancel</button
			>
		{:else}
			<button
				type="button"
				role="menuitem"
				class="btn btn-ghost btn-xs text-left text-danger"
				title="Remove this host from inventory, with its services and observation note"
				on:click={() => (forgetConfirm = true)}>Forget</button
			>
		{/if}
	{/if}
</RowMenu>

<style>
	.menu-sep {
		border-top: 1px solid var(--color-divider);
		margin: var(--space-1) 0;
	}
	/* A disabled verb keeps its word and says why underneath, in the muted
	   tone; it never just greys out (plan 3.2). */
	.menu-verb {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0;
	}
	.menu-verb:not(:disabled) {
		color: var(--color-text);
	}
	/* The global 0.4 opacity would take the reason below AA; the muted word
	   and the not-allowed cursor already say "not now". */
	.menu-verb:disabled {
		opacity: 1;
	}
	.menu-verb.text-danger:not(:disabled) {
		color: var(--color-danger);
	}
	.menu-why {
		font-size: 0.6875rem;
		line-height: 1.3;
		color: var(--color-muted);
		white-space: normal;
		font-weight: 400;
	}
</style>
