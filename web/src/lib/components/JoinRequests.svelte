<script lang="ts">
	// Hosts that are knocking on the agent hub and being turned away, and
	// bootstrap tokens nobody has used yet (lane A3, review 05/3).
	//
	// Before this, an agent installed while the enrolment window was shut was
	// refused every 30 s for days and no page said so: the refusal lived in the
	// host's journal and the audit trail. This panel is where "I installed it,
	// why is it not here?" gets its answer - and, for a host that only needs the
	// window, its one-click fix.
	import { onMount, createEventDispatcher } from 'svelte';
	import { api, type EnrolmentRequests } from '$lib/api';
	import { notify } from '$lib/stores';
	import { timeAgo } from '$lib/relativeTime';

	export let canWrite = false;

	const dispatch = createEventDispatcher<{ enrolWithBootstrap: void }>();

	let state: EnrolmentRequests | null = null;
	let opening = false;

	export async function load() {
		try {
			state = await api.getEnrolmentRequests();
		} catch (e) {
			notify('Failed to read enrolment requests: ' + String(e), 'err');
		}
	}

	async function openWindow() {
		opening = true;
		try {
			await api.openEnrolmentWindow(15);
			notify('Enrolment window open for 15 min - waiting hosts join on their next retry', 'ok');
			await load();
		} catch (e) {
			notify('Failed to open the enrolment window: ' + String(e), 'err');
		} finally {
			opening = false;
		}
	}

	function expiresIn(seconds: number): string {
		if (seconds < 60) return 'in under a minute';
		const mins = Math.floor(seconds / 60);
		if (mins < 60) return `in ${mins} min`;
		return `in ${Math.floor(mins / 60)} h ${mins % 60} min`;
	}

	onMount(load);

	$: waiting = state?.attempts.filter((a) => a.wants_to_join) ?? [];
	$: refused = state?.attempts.filter((a) => !a.wants_to_join) ?? [];
	$: tokens = state?.pending_tokens ?? [];
	$: anything = waiting.length + refused.length + tokens.length > 0;
</script>

{#if state && anything}
	<section class="card p-4 space-y-3" data-testid="join-requests" aria-labelledby="join-requests-title">
		<h2 class="section-title" id="join-requests-title">Waiting to join</h2>

		{#if waiting.length}
			<div class="space-y-2">
				{#if state.window.open}
					<p class="text-xs text-ok" data-testid="join-window-state">
						The enrolment window is open until {state.window.expires_at}. Each host below joins on
						its next retry (within 30 s); nothing needs doing on the host.
					</p>
				{:else}
					<p class="prose-note text-xs" data-testid="join-window-state">
						These hosts hold the real hub token and are refused only because no enrolment window is
						open. Opening one lets them in on their next retry - nothing needs doing on the host.
					</p>
				{/if}
				<ul class="divide-y divide-border">
					{#each waiting as a (a.hostname + a.peer)}
						<li class="py-2 flex flex-wrap items-baseline justify-between gap-2" data-testid="wants-to-join">
							<span class="text-sm text-ink">
								<span class="font-semibold">{a.hostname || '(no hostname)'}</span>
								<span class="text-muted text-xs">from {a.peer}</span>
								wants to join
							</span>
							<span class="text-xs text-muted">
								refused {a.attempts} time{a.attempts === 1 ? '' : 's'} since {timeAgo(a.first_seen)},
								last {timeAgo(a.last_seen)}
							</span>
						</li>
					{/each}
				</ul>
				{#if canWrite && !state.window.open}
					<div class="flex flex-wrap gap-2">
						<button class="btn btn-primary text-xs" on:click={openWindow} disabled={opening}>
							{opening ? 'Opening…' : 'Open enrolment window (15 min)'}
						</button>
						<button class="btn btn-ghost text-xs" on:click={() => dispatch('enrolWithBootstrap')}>
							Use a bootstrap token instead
						</button>
					</div>
				{/if}
			</div>
		{/if}

		{#if refused.length}
			<div class="space-y-1">
				<p class="field-label">Refused</p>
				<ul class="divide-y divide-border">
					{#each refused as a (a.hostname + a.peer)}
						<li class="py-2 space-y-0.5" data-testid="refused-host">
							<p class="text-sm text-ink">
								<span class="font-semibold">{a.hostname || '(no hostname)'}</span>
								<span class="text-muted text-xs">from {a.peer} -
									{a.attempts} attempt{a.attempts === 1 ? '' : 's'}, last {timeAgo(a.last_seen)}</span>
							</p>
							<p class="prose-note text-xs">{a.reason}</p>
						</li>
					{/each}
				</ul>
				<p class="prose-note text-xs">
					An open window would not let these in - each line says why. A host you do mean to add
					joins by re-running the install one-liner on it with a fresh bootstrap token.
				</p>
			</div>
		{/if}

		{#if tokens.length}
			<div class="space-y-1">
				<p class="field-label">Bootstrap tokens not used yet</p>
				<ul class="divide-y divide-border">
					{#each tokens as t (t.id)}
						<li class="py-2 text-xs flex flex-wrap justify-between gap-2" data-testid="pending-token">
							<span class="text-ink">
								token <code>{t.id}…</code>
								{#if t.minted_by}minted by {t.minted_by}{/if}
								{#if t.created_at}{timeAgo(t.created_at)}{/if}
							</span>
							{#if t.expired}
								<span class="text-warn">expired unused</span>
							{:else}
								<span class="text-muted">expires {expiresIn(t.seconds_remaining)}</span>
							{/if}
						</li>
					{/each}
				</ul>
			</div>
		{/if}
	</section>
{/if}
