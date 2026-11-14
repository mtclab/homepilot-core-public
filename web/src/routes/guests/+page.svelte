<script lang="ts">
	// Guests (3.12): friends as people. One row per friend - their machines with
	// live state and a link to each host page, their budget as bars, Invite and
	// Budget - and the invite history folded under them. Inviting and setting a
	// budget are short dialogs; the long explanation moved behind "How the
	// guest portal works". The whole page is ONE read (GET /admin/guests/friends).
	//
	// Owner on prod 3.11.0: "Guests is a bit too simple at the moment, and
	// clunky." The guest NETWORK and the provisioning defaults stay in
	// Settings > Subsystems, where they are configured.
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { api, type Friend, type FriendInvite, type GuestFriends } from '$lib/api';
	import { notify } from '$lib/stores';
	import Panel from '$lib/components/Panel.svelte';
	import FriendRow from '$lib/components/FriendRow.svelte';
	import InviteDialog from '$lib/components/InviteDialog.svelte';
	import BudgetDialog from '$lib/components/BudgetDialog.svelte';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';

	let data: GuestFriends | null = null;
	let loading = true;
	let loadError = '';

	/** The Invite dialog's pre-filled name; null = closed. */
	let inviteFor: string | null = null;
	let budgetFor: Friend | null = null;
	let revokeAsk: { cn: string; invite: FriendInvite } | null = null;
	let revoking = false;

	async function load() {
		loadError = '';
		try {
			data = await api.getGuestFriends();
		} catch (e) {
			loadError = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function onBudgetSaved(cn: string) {
		budgetFor = null;
		notify(`Budget saved for ${cn}`);
		await load();
	}

	async function confirmRevoke() {
		if (!revokeAsk) return;
		revoking = true;
		const { invite } = revokeAsk;
		try {
			await api.revokeGuestInvite(invite.prefix);
			notify(`Invite ${invite.prefix} revoked`);
			revokeAsk = null;
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			revoking = false;
		}
	}

	$: friends = data?.friends ?? [];
	$: machineCount = friends.reduce((n, f) => n + f.machines.length, 0);
	$: meta = `${friends.length} ${friends.length === 1 ? 'person' : 'people'} · ${machineCount} ${machineCount === 1 ? 'machine' : 'machines'}`;

	onMount(load);
</script>

<div class="page-stack" id="guests-panel">
	<div class="top">
		<h1 class="page-title">Guests</h1>
		{#if data && friends.length}
			<button type="button" class="btn btn-primary" on:click={() => (inviteFor = '')}>Invite a friend</button>
		{/if}
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading...</p>
	{:else if loadError}
		<p class="text-danger text-sm" role="alert">{loadError}</p>
	{:else if data}
		<Panel title={friends.length ? 'Friends' : ''} meta={friends.length ? meta : ''} flush>
			{#if friends.length}
				<ul class="friends" aria-label="Friends">
					{#each friends as friend (friend.cn)}
						<FriendRow
							{friend}
							on:invite={(e) => (inviteFor = e.detail.cn)}
							on:budget={(e) => (budgetFor = friends.find((f) => f.cn === e.detail.cn) ?? null)}
							on:revoke={(e) => (revokeAsk = e.detail)}
						/>
					{/each}
				</ul>
			{:else}
				<div class="calm" data-guests-empty>
					<p>No friends yet. A friend gets their own machine here, and a page where they see and run only that.</p>
					<button type="button" class="btn btn-primary" on:click={() => (inviteFor = '')}>Invite a friend</button>
				</div>
			{/if}
			<div class="foot">
				<details>
					<summary>How the guest portal works</summary>
					<p class="prose-note prose-measure">
						A friend signs in to the guest portal with their client certificate and sees only their own
						machines, their power buttons and their budget: nothing else of HomePilot faces them. You invite
						them here; opening the one-time link builds one machine at the size the invite carries. A budget
						caps the total across all of that friend's machines and is checked when they open an invite, so
						nobody is stopped after the fact. The guest network and the provisioning defaults are in
						<a class="text-accent hover:underline" href="{base}/settings?tab=subsystems">Settings &gt; Subsystems</a>.
					</p>
				</details>
			</div>
		</Panel>
	{/if}
</div>

{#if data && inviteFor !== null}
	<InviteDialog
		cn={inviteFor}
		{friends}
		sizing={data.sizing}
		defaults={data.defaults}
		portalOrigin={data.portal_origin}
		on:minted={load}
		on:close={() => (inviteFor = null)}
	/>
{/if}

{#if budgetFor}
	<BudgetDialog friend={budgetFor} on:saved={(e) => onBudgetSaved(e.detail.cn)} on:close={() => (budgetFor = null)} />
{/if}

{#if revokeAsk}
	<ConfirmDialog
		title="Revoke invite {revokeAsk.invite.prefix} for {revokeAsk.cn}"
		consequence="The link stops working at once and cannot be opened again. Machines {revokeAsk.cn} already has are not touched."
		confirmLabel="Revoke invite"
		busy={revoking}
		on:confirm={confirmRevoke}
		on:cancel={() => (revokeAsk = null)}
	/>
{/if}

<style>
	.top {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-3);
	}
	.calm {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--space-3);
		padding: var(--space-5) var(--space-3);
		font-family: var(--font-serif);
		font-size: 1rem;
		line-height: 1.5;
		color: var(--color-text);
	}
	.foot {
		padding: var(--space-2) var(--space-3);
		border-top: 1px solid var(--color-divider);
		font-size: 0.75rem;
		color: var(--color-muted);
	}
	.foot summary {
		cursor: pointer;
		color: var(--color-accent);
		width: max-content;
		max-width: 100%;
	}
	.foot p {
		margin-top: var(--space-2);
	}
</style>
