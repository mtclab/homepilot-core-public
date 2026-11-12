<script lang="ts">
	// Connected agents older than this control plane, or reporting no version,
	// with the one action that brings them current: Upgrade agent. It reinstalls
	// the binary and its settings through qemu-guest-agent and ends with the SAME
	// agent connected - no revoke, no enrolment window. Before this nothing
	// upgraded an enrolled agent, and revoking one to reinstall it left a dead
	// credential on the host (#815).
	import { onMount } from 'svelte';
	import { api, type AgentUpgrade } from '$lib/api';
	import { notify } from '$lib/stores';

	export let canWrite = false;

	let rows: AgentUpgrade[] = [];
	let upgrading: Record<string, boolean> = {};

	export async function load() {
		try {
			rows = await api.getAgentUpgrades();
		} catch (e) {
			notify('Failed to read agent versions: ' + String(e), 'err');
		}
	}

	async function upgrade(row: AgentUpgrade) {
		if (!row.host_id) return;
		upgrading = { ...upgrading, [row.agent_id]: true };
		try {
			const { task_id } = await api.installAgent(row.host_id, true);
			notify(`Upgrading the agent on ${row.hostname} - progress in Tasks`);
			for (;;) {
				const task = await api.getTask(task_id);
				if (task.status !== 'pending' && task.status !== 'running') {
					notify(
						task.status === 'succeeded'
							? `${row.hostname} runs the current agent`
							: `Upgrade on ${row.hostname} failed: ${task.error ?? ''}`,
						task.status === 'succeeded' ? 'ok' : 'err'
					);
					break;
				}
				await new Promise((r) => setTimeout(r, 3000));
			}
			await load();
		} catch (e) {
			notify(e instanceof Error ? e.message : String(e), 'err');
		} finally {
			upgrading = { ...upgrading, [row.agent_id]: false };
		}
	}

	onMount(load);
</script>

{#if rows.length}
	<section class="card p-4 space-y-3" data-testid="agent-upgrades" aria-labelledby="agent-upgrades-title">
		<h2 class="section-title" id="agent-upgrades-title">Agents to upgrade</h2>
		<p class="prose-note text-xs">
			These hosts run an agent older than this HomePilot ({rows[0].control_version}), or one that
			does not say which version it is. Fixes shipped in the agent have not reached them. Upgrade
			keeps the same agent enrolled; if the new one does not connect, the previous one is put back.
		</p>
		<ul class="divide-y divide-border">
			{#each rows as r (r.agent_id)}
				<li class="py-2 flex flex-wrap items-baseline justify-between gap-2" data-testid="agent-upgrade">
					<span class="text-sm text-ink">
						<span class="font-semibold">{r.hostname}</span>
						<span class="text-muted text-xs">{r.agent_version ?? 'no version reported'}</span>
					</span>
					{#if canWrite && r.host_id}
						<button class="btn btn-ghost text-xs"
							title="Reinstall the current agent through qemu-guest-agent. The same agent stays enrolled; no enrolment window is opened."
							disabled={upgrading[r.agent_id]}
							on:click={() => upgrade(r)}>
							{upgrading[r.agent_id] ? 'Upgrading…' : 'Upgrade agent'}
						</button>
					{:else if !r.host_id}
						<span class="text-xs text-muted">not a QEMU guest HomePilot can reach - re-run the install one-liner on it</span>
					{/if}
				</li>
			{/each}
		</ul>
	</section>
{/if}
