<script lang="ts">
	// What a guest-disk change (#824) would do, read live when the change is
	// opened: the slot from the guest's CURRENT config, the serial the guest
	// will find the disk by, the fstab line, every in-guest step, and anything
	// that would refuse it now. Read-only: the plan sends nothing to the guest
	// but a read-only check.
	import { onMount } from 'svelte';
	import { planDiskChange, type DiskPlan } from '$lib/api';

	export let artifactId: string;

	let plan: DiskPlan | null = null;
	let summary = '';
	let unread = '';
	let loading = true;

	async function load() {
		loading = true;
		try {
			const got = await planDiskChange(artifactId);
			plan = got.disk ?? null;
			summary = got.summary;
			unread = '';
		} catch (e) {
			// "We could not tell you what this does" stays next to the buttons.
			unread = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(() => void load());
</script>

<div class="space-y-2 text-sm" data-testid="disk-plan">
	<p class="text-xs text-muted font-medium">What applying it does now</p>
	{#if loading}
		<p class="prose-note">Reading the guest from Proxmox…</p>
	{:else if unread}
		<p class="text-danger">Cannot show what this would do: {unread}</p>
		<button type="button" class="btn btn-ghost text-xs" on:click={load}>Retry</button>
	{:else if plan}
		<p class="text-ink" data-testid="disk-plan-summary">{summary}</p>
		<dl class="grid gap-x-6 gap-y-1 text-xs" style="grid-template-columns: max-content 1fr;">
			<dt class="text-muted">Slot</dt>
			<dd class="m-0 font-mono" data-testid="disk-plan-slot">{plan.slot ?? 'none free'}</dd>
			<dt class="text-muted">Disk</dt>
			<dd class="m-0">{plan.size_gib} GiB from {plan.storage}, serial <span class="font-mono">{plan.serial}</span></dd>
			<dt class="text-muted">Filesystem</dt>
			<dd class="m-0">{plan.filesystem}, label <span class="font-mono">{plan.label}</span></dd>
			<dt class="text-muted">fstab line</dt>
			<dd class="m-0 font-mono break-all">{plan.fstab_line}</dd>
		</dl>
		<ol class="list-decimal pl-5 text-xs text-muted">
			<li>Attach the disk ({plan.bus}, hot-plugged)</li>
			{#each plan.guest_steps as s (s)}<li>{s}</li>{/each}
		</ol>
		{#if plan.blockers.length}
			<ul class="text-danger text-xs list-disc pl-5" data-testid="disk-plan-blockers">
				{#each plan.blockers as b (b)}<li>{b}</li>{/each}
			</ul>
		{/if}
	{/if}
</div>
