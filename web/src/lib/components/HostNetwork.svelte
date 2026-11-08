<script lang="ts">
	// The host's Network tab (lane B2, review 02 §3.3): every address the machine
	// holds, where each one came from and how old that is, and every other
	// machine that holds one of them. Read-only. Self-contained like AlertsPanel:
	// the host page drops it in with one line and it owns its loading, its
	// unread and its empty states.
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { api, type AddressConflict, type HostAddress } from '$lib/api';
	import { timeAgo } from '$lib/relativeTime';
	import { isObserved, sourceLabel, wireText } from '$lib/addresses';
	import AttentionItem from '$lib/components/AttentionItem.svelte';

	export let hostId: string;

	let addresses: HostAddress[] = [];
	let conflicts: AddressConflict[] = [];
	let loaded = false;
	let unread = '';

	async function load() {
		try {
			const res = await api.getHostNetwork(hostId);
			addresses = res.addresses ?? [];
			conflicts = res.conflicts ?? [];
			unread = '';
		} catch (e) {
			// Never "no addresses" when the answer is "could not ask".
			unread = e instanceof Error ? e.message : String(e);
		} finally {
			loaded = true;
		}
	}

	onMount(() => void load());

	function otherHost(c: AddressConflict): string {
		const other = c.host_ids.find((id) => id !== hostId);
		return other ? `${base}/hosts/${other}?tab=network` : '';
	}
</script>

<div class="space-y-4" data-host-network>
	{#if conflicts.length}
		<section class="card p-4 space-y-2" aria-labelledby="network-conflicts">
			<h2 class="section-title" id="network-conflicts">Address clashes</h2>
			{#each conflicts as c (c.address)}
				<AttentionItem
					severity="critical"
					label="address clash"
					text={`${c.summary}. ${c.remedy}`}
					href={otherHost(c)}
				/>
			{/each}
		</section>
	{/if}

	<section class="card p-4 space-y-2" aria-labelledby="network-addresses">
		<h2 class="section-title" id="network-addresses">Addresses</h2>
		{#if !loaded}
			<p class="text-muted text-sm">Reading addresses...</p>
		{:else if unread}
			<p class="text-danger text-sm">
				Could not read this host's addresses: {unread}
				<button class="text-accent text-xs ml-1" on:click={() => void load()}>Retry</button>
			</p>
		{:else if addresses.length}
			<table class="data-table w-full text-sm">
				<thead>
					<tr>
						<th class="text-left col-primary">Address</th>
						<th class="text-left col-secondary">Interface</th>
						<th class="text-left col-secondary">MAC</th>
						<th class="text-left col-secondary">Source</th>
						<th class="text-left col-secondary">Read</th>
					</tr>
				</thead>
				<tbody>
					{#each addresses as a (`${a.source}:${a.iface}:${a.address}`)}
						<tr data-address={a.address}>
							<td class="col-primary num-inline">
								{a.address}{a.prefixlen != null ? `/${a.prefixlen}` : ''}
								{#if conflicts.some((c) => c.address === a.address)}
									<span class="badge badge-critical ml-1">clash</span>
								{/if}
							</td>
							<td class="col-secondary">{wireText(a) || '-'}</td>
							<td class="col-secondary font-mono text-xs">{a.mac ?? '-'}</td>
							<td class="col-secondary">{sourceLabel(a.source)}</td>
							<td class="col-secondary">
								{isObserved(a.source) ? `seen ${timeAgo(a.observed_at)}` : timeAgo(a.observed_at)}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
			<p class="text-muted text-xs">
				Configured addresses come from the guest's config at each sync; observed ones are what its
				guest agent last reported.
			</p>
		{:else}
			<p class="text-muted text-sm">
				No address is known for this host yet: its config names no static address and no guest
				agent has reported one.
			</p>
		{/if}
	</section>
</div>
