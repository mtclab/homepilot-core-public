<script lang="ts">
	// Where a change stands and how its last run ended, in words (owner
	// feedback 2026-11-08). Ready to run says so; a run says "Running: <intent>,
	// started 18:28" with the task's live state; a finished run says how it
	// ended - partly applied named in full (#796), PVE warnings shown verbatim
	// with a one-line explanation and a harmless / needs-attention / not
	// recognised verdict, never just "WARNINGS: 1".
	import type { ActiveTask, Artifact, LastTask } from '$lib/api';
	import Lamp from '$lib/components/Lamp.svelte';
	import Tag from '$lib/components/Tag.svelte';
	import {
		inboxState,
		outcomeSentence,
		runningLine,
		STATE_LABEL,
		STATE_LAMP,
		warningsOf,
		warningVerdict,
	} from '$lib/changes';

	export let fm: Artifact;
	export let activeTask: ActiveTask | null | undefined = null;
	export let lastTask: LastTask | null | undefined = null;

	$: state = inboxState(fm, activeTask);
	$: sentence = outcomeSentence(fm, lastTask);
	$: warnings = state === 'running' ? [] : warningsOf(lastTask);
	$: partial = fm.partial_apply;
</script>

<div class="space-y-1 text-xs" data-testid="change-outcome" data-state={state}>
	{#if STATE_LABEL[state]}
		<div class="flex items-center gap-2 flex-wrap">
			<Lamp state={STATE_LAMP[state]} label={STATE_LABEL[state]} class="text-ink-strong" />
			{#if state === 'running' && activeTask}
				<span class="text-muted" data-testid="running-line"
					>{runningLine(fm.intent, activeTask.created_at)} · task {activeTask.status}</span
				>
			{:else if state === 'ready'}
				<span class="text-muted">Approved; it runs when you press Apply.</span>
			{:else if state === 'review'}
				<span class="text-muted">Waiting for a decision.</span>
			{/if}
		</div>
	{/if}

	{#if sentence && state !== 'running'}
		<p class="prose-note" data-testid="outcome-sentence">{sentence}</p>
	{/if}

	{#if partial && state === 'partial'}
		<ul class="text-muted space-y-0.5" data-testid="partial-apply">
			{#if partial.steps_done.length}
				<li>Done: {partial.steps_done.join(', ')}</li>
			{/if}
			{#if partial.keys_applied?.length}
				<li>Changed before the failure: {partial.keys_applied.join(', ')}</li>
			{/if}
			{#if partial.steps_failed.length}
				<li>Failed: {partial.steps_failed.join(', ')}</li>
			{/if}
			{#if partial.keys_not_applied?.length}
				<li>Not changed: {partial.keys_not_applied.join(', ')}</li>
			{/if}
			{#if partial.steps_not_run.length}
				<li>Not run: {partial.steps_not_run.join(', ')}</li>
			{/if}
		</ul>
	{/if}

	{#if warnings.length}
		<ul class="space-y-1" data-testid="pve-warnings">
			{#each warnings as w}
				<li class="border-l-2 border-warn-border pl-2 space-y-0.5">
					<div class="flex items-center gap-2 flex-wrap">
						<Tag tone={w.harmless === true ? 'ok' : w.harmless === false ? 'crit' : 'warn'}
							>{warningVerdict(w)}</Tag
						>
						<span class="text-muted">Proxmox warning</span>
					</div>
					{#if w.explanation}<p class="prose-note">{w.explanation}</p>{/if}
					<p class="font-mono text-ink break-words">{w.text}</p>
				</li>
			{/each}
		</ul>
	{/if}
</div>
