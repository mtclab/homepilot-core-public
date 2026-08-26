<script lang="ts">
	// Drift, attention-first (#549 F4).
	//
	// This page used to print every active artifact across two tables: the forty
	// that agree with reality next to the two that do not, each with its own
	// green tick. The page whose whole job is "these two need you" said it in a
	// wall of ✓. Now only DISAGREEMENT is enumerated — drifted artifacts, and
	// checks that established nothing (#425) — and everything healthy collapses
	// into one line. The rollup itself lives in `$lib/drift` so the rule can be
	// asserted without a DOM.
	import { onMount, onDestroy } from 'svelte';
	import { api, type Artifact, type Host, type DriftCheck } from '$lib/api';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';
	import { onArtifactEvent } from '$lib/events';
	import { debounce } from '$lib/debounce';
	import { buildDriftRollup } from '$lib/drift';
	import { timeAgo } from '$lib/relativeTime';
	import DriftItemCard from '$lib/components/DriftItemCard.svelte';

	let artifacts: Artifact[] = [];
	let hosts: Host[] = [];
	let driftChecks: DriftCheck[] = [];
	let loading = true;
	let loadError = '';
	let rechecking = false;
	let recheckingId = '';

	$: rollup = buildDriftRollup(artifacts, hosts, driftChecks);

	async function load(initial = true) {
		if (initial) loading = true;
		loadError = '';
		try {
			const [aRes, hRes, dRes] = await Promise.all([
				api.listArtifacts({ limit: 500 }),
				api.listInventory(),
				api.getDriftStatus({ limit: 500 }),
			]);
			artifacts = aRes.items;
			hosts = hRes.items;
			driftChecks = dRes.items;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			// On a silent live refresh, keep the last-good page rather than
			// replacing it with the error card / a toast.
			if (initial || artifacts.length === 0) {
				loadError = msg;
				notify(msg, 'err');
			}
		} finally {
			loading = false;
		}
	}

	async function recheck(artifactId: string) {
		rechecking = true;
		recheckingId = artifactId;
		try {
			const res = await api.recheckDrift(artifactId);
			if (res.items.length > 0) {
				driftChecks = [
					...driftChecks.filter((dc) => dc.artifact_id !== artifactId),
					res.items[0],
				];
			}
			notify('Drift recheck complete', 'ok');
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			rechecking = false;
			recheckingId = '';
		}
	}

	let showUncovered = false;

	onMount(load);
	// Live-refresh on drift + lifecycle events (an apply/revoke changes what's
	// covered; artifact_drifted changes an item's state). Debounced hard: EVERY
	// event here fired three parallel calls (500 artifacts + full inventory +
	// 500 drift rows), so a burst of events was a burst of full reloads.
	const refresh = debounce(() => load(false), 400);
	const unsub = onArtifactEvent(refresh);
	onDestroy(() => {
		unsub();
		refresh.cancel();
	});
</script>

<div class="section-stack">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Drift</h1>
		<button class="btn btn-ghost text-xs" on:click={() => load(true)}>↻ Refresh</button>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load drift data.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={() => load(true)}>↻ Retry</button>
		</div>
	{:else if rollup.total === 0}
		<p class="prose-note text-sm">
			No active artifacts — nothing has been applied yet, so there is nothing to drift.
		</p>
	{:else}
		<!-- The honest header (#425). With nothing checked there is no percentage
		     to quote: a coverage figure computed over zero checks is the exact
		     "100% healthy" lie the issue is about. -->
		{#if rollup.checkedCount === 0}
			<p class="prose-note text-sm" data-testid="drift-coverage">
				Nothing checked yet — {rollup.total} active artifact{rollup.total === 1 ? '' : 's'} have never
				been drift-checked.
			</p>
		{:else}
			<p class="prose-note text-sm" data-testid="drift-coverage">
				{rollup.checkedCount} of {rollup.total} active artifacts checked ({rollup.coveragePct}%).
			</p>
		{/if}

		{#if rollup.drifted.length > 0}
			<section class="section-stack" data-testid="drift-attention">
				<h2 class="section-title text-danger">
					⚠ {rollup.drifted.length} artifact{rollup.drifted.length === 1 ? '' : 's'} disagree{rollup
						.drifted.length === 1
						? 's'
						: ''} with reality
				</h2>
				{#each rollup.drifted as item (item.artifact.id)}
					<DriftItemCard
						{item}
						rechecking={rechecking && recheckingId === item.artifact.id}
						onRecheck={recheck}
					/>
				{/each}
			</section>
		{/if}

		{#if rollup.unresolved.length > 0}
			<!-- An errored check is NOT "in spec" (#425). It gets its own section
			     and its own reason, because the operator's next step is different:
			     nothing is known about these, and no one is going to find out by
			     waiting. -->
			<section class="section-stack" data-testid="drift-unresolved">
				<h2 class="section-title text-warn">
					? {rollup.unresolved.length} check{rollup.unresolved.length === 1 ? '' : 's'} established
					nothing
				</h2>
				{#each rollup.unresolved as item (item.artifact.id)}
					<DriftItemCard
						{item}
						summaryPrefix="Not established:"
						rechecking={rechecking && recheckingId === item.artifact.id}
						onRecheck={recheck}
					/>
				{/each}
			</section>
		{/if}

		<!-- Everything healthy is ONE line, on purpose. Naming the forty that
		     agree is what buried the two that do not. -->
		<div class="card space-y-1">
			{#if rollup.inSpec.count > 0}
				<p class="text-sm text-ok" data-testid="drift-in-spec-summary">
					✓ {rollup.inSpec.count} in spec, last checked {timeAgo(rollup.inSpec.lastCheckedAt)}
				</p>
			{/if}
			{#if rollup.uncheckedCount > 0}
				<p class="text-sm text-muted" data-testid="drift-unchecked-summary">
					— {rollup.uncheckedCount} not checked yet
				</p>
			{/if}
			{#if rollup.orphanCount > 0}
				<p class="text-xs text-warn" data-testid="drift-orphan-summary">
					{rollup.orphanCount} active artifact{rollup.orphanCount === 1 ? '' : 's'} target a name inventory
					does not know.
				</p>
			{/if}
			{#if rollup.drifted.length === 0 && rollup.unresolved.length === 0 && rollup.checkedCount > 0}
				<p class="prose-note text-xs">Nothing disagrees with reality.</p>
			{/if}
		</div>

		{#if rollup.uncoveredHosts.length > 0}
			<div class="card space-y-2">
				<button
					class="section-title text-muted text-left"
					aria-expanded={showUncovered}
					on:click={() => (showUncovered = !showUncovered)}
				>
					{showUncovered ? '▾' : '▸'} ○ {rollup.uncoveredHosts.length} hosts covered by no artifact
				</button>
				{#if showUncovered}
					<p class="prose-note text-xs">
						In inventory but not targeted by any active artifact — config drift can't be tracked for
						them. Adopting a host in inventory does not cover it; an artifact must target it. Not
						related to the inventory "managed" flag.
					</p>
					<div class="flex flex-wrap gap-2">
						{#each rollup.uncoveredHosts as h (h.id)}
							<a
								href="{base}/inventory/{h.id}"
								class="px-2 py-1 bg-raised hover:bg-border rounded text-xs font-mono text-ink transition-colors"
							>
								{h.hostname}
							</a>
						{/each}
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</div>
