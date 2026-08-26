<script lang="ts">
	// The guest network, told in three parts: what the operator asked for, what
	// the cluster has, and the difference (#553).
	//
	// There is deliberately NO apply button here. The change ships as a
	// `guest-network` artifact - propose, approve with the code, apply - so the
	// record of who decided to rebuild the guest subnet lives in the artifact
	// store rather than in a click nobody can find afterwards. This card's job is
	// to make that decision an informed one, and to point at where it happens.
	//
	// The legacy-stack caveat is on the card rather than in the docs because it
	// is the difference between a fence that holds and a page that says it does:
	// on the iptables stack PVE stores vnet firewall rules and does not apply
	// them, and the per-VM rules written at provision time are what actually
	// fences a guest.
	import { onMount } from 'svelte';
	import { api, type GuestNetworkReport, type SettingOverride } from '$lib/api';
	import { GUEST_NETWORK_KEYS, settingsFor } from '$lib/settingFields';
	import SettingFields from './SettingFields.svelte';

	export let overrides: SettingOverride[] = [];
	export let canWrite = false;
	export let onSaved: () => void = () => {};

	let report: GuestNetworkReport | null = null;
	let error = '';
	let loading = false;

	export async function load(): Promise<void> {
		loading = true;
		error = '';
		try {
			report = await api.getGuestNetwork();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(load);

	$: steps = report?.plan?.steps ?? [];
	$: blockers = report?.plan?.blockers ?? [];
	$: converged = report?.plan?.converged === true;
</script>

<div class="card space-y-3" data-guest-network>
	<div class="flex items-baseline justify-between gap-3 flex-wrap">
		<h2 class="section-title">Guest network</h2>
		<!-- Named for what it re-checks: the tab already has a "Re-check" for the
		     self-check report, and two identically-labelled buttons on one screen
		     is an operator guessing which one they pressed. -->
		<button class="btn btn-ghost btn-sm" disabled={loading} on:click={load}>
			{loading ? 'Surveying…' : '↻ Re-survey the cluster'}
		</button>
	</div>

	<p class="prose-note prose-measure text-xs">
		The subnet a friend's machine lives on: its own SDN zone and vnet, a gateway
		that gives it the internet, DHCP from the node, and a list of networks it
		must never reach. HomePilot writes that last list as firewall rules on every
		guest it provisions onto this vnet — that is the fence.
	</p>

	{#if error}
		<p class="text-xs text-danger" data-guest-network-error>
			Could not read the guest network: {error}
		</p>
	{:else if report}
		<!-- State line. One sentence, and it is the server's own, so the card and
		     the API can never describe the estate differently. -->
		<p class="text-xs" data-guest-network-state class:text-ok={converged} class:text-danger={blockers.length}>
			{#if !report.configured}
				Not described yet — {report.detail}
			{:else if blockers.length}
				Cannot proceed — {report.detail}
			{:else if converged}
				Converged: the cluster matches the desired guest network.
			{:else if report.plan}
				{steps.length} step{steps.length === 1 ? '' : 's'} pending.
			{:else}
				{report.detail}
			{/if}
		</p>

		{#if report.enforcement}
			<p class="prose-note prose-measure text-xs" data-guest-network-enforcement>
				{report.enforcement}
			</p>
		{/if}

		{#if steps.length}
			<ol class="space-y-1 text-xs" data-guest-network-plan>
				{#each steps as step (step.id)}
					<li class="flex gap-2">
						<span class="font-mono text-muted shrink-0">{step.id}</span>
						<span class="text-ink">{step.description}</span>
					</li>
				{/each}
			</ol>
		{/if}

		{#if report.survey?.errors?.length}
			<p class="text-xs text-danger">
				Parts of the cluster could not be read, so the plan above is incomplete:
				{report.survey.errors.join('; ')}
			</p>
		{/if}

		{#if report.configured}
			<p class="prose-note prose-measure text-xs">
				Changes ship as a change record, not from this card: propose a
				<span class="font-mono">guest-network</span> artifact, approve it with the
				code, and applying it runs exactly the plan above.
				<a class="link" href="/ui/changes/review">Go to the review queue →</a>
			</p>
		{/if}
	{:else if loading}
		<p class="text-xs text-muted">Checking…</p>
	{/if}

	<SettingFields
		settings={settingsFor(overrides, GUEST_NETWORK_KEYS)}
		{canWrite}
		onSaved={() => {
			onSaved();
			load();
		}}
	/>
</div>
