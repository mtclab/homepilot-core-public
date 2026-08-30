<script lang="ts">
	// Alert rules (#514 S4): configuration, so it lives in Settings → Monitoring.
	// It used to be a toolbar button on the Agents tab, which is how monitoring
	// config ended up hiding inside a fleet-credential page. Firing alerts still
	// show where the operator looks: Overview and the affected host.
	import { onMount } from 'svelte';
	import { api, type AlertComparison, type AlertRule } from '$lib/api';
	import { notify } from '$lib/stores';
	import { metricLabel } from '$lib/sparkline';

	const RULE_METRICS = ['load.1m', 'load.5m', 'load.15m', 'memory.free_gb', 'disk.free_gb'];

	let rules: AlertRule[] = [];
	let savingRule = false;
	// The duration is entered in MINUTES because that is how an operator thinks
	// about "don't page me for a blip"; the API takes seconds.
	let draft = {
		name: '',
		metric: 'load.1m',
		comparison: 'gt' as AlertComparison,
		threshold: 4,
		for_minutes: 5,
		host_filter: '*'
	};
	const COMPARISON_LABELS: Record<AlertComparison, string> = {
		gt: 'above',
		gte: 'at or above',
		lt: 'below',
		lte: 'at or below'
	};

	async function loadRules() {
		try {
			rules = (await api.listAlertRules()).items;
		} catch (e) {
			notify('Could not load alert rules: ' + String(e), 'err');
		}
	}

	async function createRule() {
		if (!draft.name.trim()) {
			notify('Give the rule a name', 'err');
			return;
		}
		savingRule = true;
		try {
			await api.createAlertRule({
				name: draft.name.trim(),
				metric: draft.metric,
				comparison: draft.comparison,
				threshold: Number(draft.threshold),
				for_seconds: Math.round(Number(draft.for_minutes) * 60),
				host_filter: draft.host_filter.trim() || '*'
			});
			draft = { ...draft, name: '' };
			await loadRules();
			notify('Alert rule created', 'ok');
		} catch (e) {
			notify('Could not create the rule: ' + String(e), 'err');
		} finally {
			savingRule = false;
		}
	}

	async function toggleRule(rule: AlertRule) {
		try {
			await api.setAlertRuleEnabled(rule.id, !rule.enabled);
			await loadRules();
		} catch (e) {
			notify('Could not update the rule: ' + String(e), 'err');
		}
	}

	// Retune an existing rule in place (#593). Silencing was the only edit before;
	// changing a threshold meant delete-and-recreate, which dropped firing state.
	let editingId: string | null = null;
	let edit = { comparison: 'gt' as AlertComparison, threshold: 0, for_minutes: 0 };

	function startEdit(rule: AlertRule) {
		editingId = rule.id;
		edit = {
			comparison: rule.comparison,
			threshold: rule.threshold,
			for_minutes: Math.round(rule.for_seconds / 60)
		};
	}

	function cancelEdit() {
		editingId = null;
	}

	async function saveEdit(rule: AlertRule) {
		savingRule = true;
		try {
			await api.updateAlertRule(rule.id, {
				comparison: edit.comparison,
				threshold: Number(edit.threshold),
				for_seconds: Math.round(Number(edit.for_minutes) * 60)
			});
			editingId = null;
			await loadRules();
			notify('Alert rule updated', 'ok');
		} catch (e) {
			notify('Could not update the rule: ' + String(e), 'err');
		} finally {
			savingRule = false;
		}
	}

	async function removeRule(rule: AlertRule) {
		try {
			await api.deleteAlertRule(rule.id);
			await loadRules();
		} catch (e) {
			notify('Could not delete the rule: ' + String(e), 'err');
		}
	}

	onMount(loadRules);
</script>

<div class="space-y-4">
	<p class="prose-note prose-measure text-xs">
		An alert rule watches one metric on one host - or on all of them - and fires
		only when its condition has held for the whole duration, so a single spike
		never raises one. Firing and recovery both go out as events; firing alerts
		show on the Overview and on the affected host. There is no mail and no
		pager: to get an alert off this box, point the events webhook at something
		(Settings → Subsystems). Until you do, a firing alert reaches the log and
		this console and nowhere else, and it leaves no record once it resolves.
	</p>
	<!-- One line instead of six ragged notes under six narrow inputs: the row
	     already reads left to right, so saying so explains every field at once
	     without turning a compact form into a wall (#549 F7). -->
	<p class="prose-note prose-measure text-xs" data-alert-form-note>
		Read the row as a sentence: <em>name</em> fires when <em>metric</em> is
		<em>above/below</em> <em>threshold</em> for <em>minutes</em> on
		<em>host</em> - where <span class="font-mono">*</span> means every host, and
		a name is what you will see when it fires. <em>Edit</em> retunes an existing
		rule's comparison, threshold and duration in place - no need to delete and
		recreate it. <em>Host</em> is a glob: <span class="font-mono">*</span> is
		every host, <span class="font-mono">web-*</span> every host whose name
		starts that way, and a bare name is one machine. The Hosts column says how
		many the rule matched when it was last evaluated - if that is zero, it is
		enabled and watching nothing.
	</p>

	<div class="flex flex-wrap items-end gap-2">
		<label class="flex flex-col gap-1">
			<span class="field-label">Name</span>
			<input class="input w-44" bind:value={draft.name} placeholder="Load too high" />
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">Metric</span>
			<select class="input" bind:value={draft.metric}>
				{#each RULE_METRICS as m (m)}
					<option value={m}>{metricLabel(m)}</option>
				{/each}
			</select>
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">Is</span>
			<select class="input" bind:value={draft.comparison}>
				{#each Object.entries(COMPARISON_LABELS) as [value, label] (value)}
					<option {value}>{label}</option>
				{/each}
			</select>
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">Threshold</span>
			<input class="input num w-20" type="number" step="0.1" bind:value={draft.threshold} />
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">For (minutes)</span>
			<input class="input num w-20" type="number" min="0" step="1" bind:value={draft.for_minutes} />
		</label>
		<label class="flex flex-col gap-1">
			<span class="field-label">Host (* = all)</span>
			<input class="input w-32" bind:value={draft.host_filter} />
		</label>
		<button class="btn btn-primary text-xs" on:click={createRule} disabled={savingRule}>
			{savingRule ? 'Saving…' : 'Add rule'}
		</button>
	</div>

	{#if rules.length === 0}
		<p class="prose-note text-xs">No alert rules yet.</p>
	{:else}
		<table class="data-table text-xs">
			<thead>
				<tr>
					<th class="text-left">Rule</th>
					<th class="text-left">Condition</th>
					<th class="text-left">Hosts</th>
					<th class="text-left">State</th>
					<th class="text-left">Actions</th>
				</tr>
			</thead>
			<tbody>
				{#each rules as r (r.id)}
					<tr class="border-b border-divider">
						<td class="text-ink">{r.name}</td>
						{#if editingId === r.id}
							<!-- Retune the condition in place: same sentence, now editable.
							     Metric and host stay fixed here - change those by making a new
							     rule - but the numbers you actually tune are inline (#593). -->
							<td class="text-muted">
								{metricLabel(r.metric)}
								<select class="input" bind:value={edit.comparison} aria-label="Comparison">
									{#each Object.entries(COMPARISON_LABELS) as [value, label] (value)}
										<option {value}>{label}</option>
									{/each}
								</select>
								<input
									class="input num w-20"
									type="number"
									step="0.1"
									bind:value={edit.threshold}
									aria-label="Threshold"
								/>
								for
								<input
									class="input num w-16"
									type="number"
									min="0"
									step="1"
									bind:value={edit.for_minutes}
									aria-label="For (minutes)"
								/>
								min
							</td>
						{:else}
							<td class="text-muted">
								{metricLabel(r.metric)} {COMPARISON_LABELS[r.comparison]}
								<span class="num-inline text-ink">{r.threshold}</span>
								for {Math.round(r.for_seconds / 60)} min
							</td>
						{/if}
						<!-- The filter AND what it actually matched. A rule whose glob
						     matches no host, or whose metric no agent reports, is enabled
						     and listed and guarding nothing; before this column it looked
						     identical to one watching the whole fleet (#648 tranche 5). -->
						<td class="text-muted font-mono">
							{r.host_filter}
							{#if r.last_eval_at === null}
								<span class="font-sans text-muted" data-rule-coverage>not evaluated yet</span>
							{:else if (r.hosts_matched ?? 0) === 0}
								<span class="font-sans text-warn" data-rule-coverage>watching no host</span>
							{:else}
								<span class="font-sans text-muted" data-rule-coverage>
									watching {r.hosts_matched}
									{r.hosts_matched === 1 ? 'host' : 'hosts'}
								</span>
							{/if}
						</td>
						<td>
							{#if r.enabled}
								<span class="text-ok">enabled</span>
							{:else}
								<span class="text-muted">silenced</span>
							{/if}
						</td>
						<td class="space-x-1">
							{#if editingId === r.id}
								<button
									class="btn btn-primary btn-xs"
									on:click={() => saveEdit(r)}
									disabled={savingRule}
								>
									{savingRule ? 'Saving…' : 'Save'}
								</button>
								<button class="btn btn-ghost btn-xs" on:click={cancelEdit}>Cancel</button>
							{:else}
								<button class="btn btn-ghost btn-xs" on:click={() => startEdit(r)}>Edit</button>
								<button class="btn btn-ghost btn-xs" on:click={() => toggleRule(r)}>
									{r.enabled ? 'Silence' : 'Enable'}
								</button>
								<button class="btn btn-ghost btn-xs" on:click={() => removeRule(r)}>Delete</button>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>
