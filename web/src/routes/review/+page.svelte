<script lang="ts">
	import { onMount } from 'svelte';
	import { api, sessionStore, type Artifact } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import { notify } from '$lib/stores';
	import { base } from '$app/paths';

	let items: Artifact[] = [];
	let loading = true;
	let loadError = '';
	// Approve/Reject 403 without write scope. Default-deny while loading.
	$: canWrite = capCanWrite($sessionStore?.capabilities);
	let working: Record<string, boolean> = {};
	let expanded: string | null = null;
	let bodies: Record<string, string> = {};
	let rejectReasons: Record<string, string> = {};

	const STATUS_CLASSES: Record<string, string> = {
		proposed: 'badge-proposed',
		approved: 'badge-approved',
		applied: 'badge-applied',
		rejected: 'badge-rejected',
		revoked: 'badge-revoked',
		failed: 'badge-failed',
		superseded: 'badge-superseded',
	};
	function statusClass(s: string): string {
		return STATUS_CLASSES[s] ?? 'badge-proposed';
	}

	async function load() {
		loading = true;
		loadError = '';
		try {
			const res = await api.listArtifacts({ status: 'proposed', limit: 200 });
			items = res.items;
		} catch (e) {
			// A toast alone left the last-good (or empty) list on screen with no way
			// back — the queue looked empty when it had simply failed to load.
			const msg = e instanceof Error ? e.message : String(e);
			if (items.length === 0) loadError = msg;
			else notify(msg, 'err');
		} finally {
			loading = false;
		}
	}

	async function expand(id: string) {
		if (expanded === id) { expanded = null; return; }
		expanded = id;
		if (!bodies[id]) {
			try {
				const res = await api.getArtifact(id);
				bodies[id] = res.body;
			} catch {
				bodies[id] = '(failed to load body)';
			}
		}
	}

	async function approve(id: string) {
		if (!confirm(`Approve artifact ${id.slice(-8)}? This will allow it to be applied.`)) return;
		working = { ...working, [id]: true };
		try {
			await api.approveArtifact(id);
			notify(`Approved ${id.slice(-8)}`);
			items = items.filter((a) => a.id !== id);
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = { ...working, [id]: false };
		}
	}

	async function reject(id: string) {
		const reason = (rejectReasons[id] ?? '').trim() || undefined;
		if (!confirm(`Reject artifact ${id.slice(-8)}?${reason ? ` Reason: "${reason}"` : ''}`)) return;
		working = { ...working, [id]: true };
		try {
			await api.rejectArtifact(id, 'web', reason);
			notify(`Rejected ${id.slice(-8)}`);
			items = items.filter((a) => a.id !== id);
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			working = { ...working, [id]: false };
			delete rejectReasons[id];
		}
	}

	function targetStr(a: Artifact): string {
		const t = a.target ?? {};
		return t.host ?? t.service ?? t.node ?? '—';
	}

	onMount(load);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Review Queue</h1>
		<div class="flex items-center gap-3">
			{#if !canWrite && !loading && !loadError}
				<span class="text-muted text-xs">Read-only session — approving and rejecting need a write-scope token.</span>
			{/if}
			<span class="text-muted text-xs">{items.length} proposed</span>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Refresh</button>
		</div>
	</div>

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load the review queue.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Retry</button>
		</div>
	{:else if items.length === 0}
		<div class="card text-center py-10">
			<p class="prose-note text-sm">Queue empty — nothing to review.</p>
		</div>
	{:else}
		<div class="space-y-3">
			{#each items as a}
				<div class="card space-y-3">
					<!-- Header row -->
					<div class="flex items-start gap-3">
						<button
							class="text-muted hover:text-ink-strong text-xs mt-0.5 w-4 shrink-0"
							on:click={() => expand(a.id)}
						>
							{expanded === a.id ? '▼' : '▶'}
						</button>
						<div class="flex-1 min-w-0">
							<a
								href="{base}/artifacts/{a.id}"
								class="text-xs text-accent hover:text-accent-strong font-mono"
								title="Open artifact detail"
							>{a.id}</a>
							<p class="text-sm text-ink-strong font-medium mt-0.5 truncate">{a.intent}</p>
							<div class="flex gap-2 mt-1 flex-wrap text-xs text-muted">
								<span>{a.kind}</span>
								<span>·</span>
								<span>{targetStr(a)}</span>
								{#if a.tags?.length}
									<span>·</span>
									{#each a.tags as tag}
										<span class="text-muted">{tag}</span>
									{/each}
								{/if}
							</div>
						</div>
						<!-- Actions -->
						{#if canWrite}
							<div class="flex gap-2 shrink-0">
								<button
									class="btn btn-success text-xs"
									disabled={working[a.id]}
									on:click={() => approve(a.id)}
								>✓ Approve</button>
								<button
									class="btn btn-danger text-xs"
									disabled={working[a.id]}
									on:click={() => reject(a.id)}
								>✗ Reject</button>
							</div>
						{/if}
					</div>

					<!-- Expanded body -->
					{#if expanded === a.id}
						<div class="ml-7 space-y-2">
							{#if bodies[a.id]}
								<pre class="code-block text-xs overflow-x-auto whitespace-pre-wrap max-h-64">{bodies[a.id]}</pre>
							{:else}
								<p class="text-muted text-xs">Loading…</p>
							{/if}
							{#if canWrite}
								<div class="flex gap-2 items-center">
									<input
										class="input text-xs flex-1"
										placeholder="Rejection reason (optional)"
										bind:value={rejectReasons[a.id]}
									/>
									<button
										class="btn btn-danger text-xs"
										disabled={working[a.id]}
										on:click={() => reject(a.id)}
									>Reject with reason</button>
								</div>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>
