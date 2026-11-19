<script context="module" lang="ts">
	// The structured policy rules a proposed change matches (#506), as the server
	// reports them on GET /artifacts/{id} (`policy_warnings`). A match is never a
	// refusal of the change: approving it needs the policy ACKNOWLEDGED, and the
	// acknowledgement is recorded on the approval. A rule that does not parse is
	// said here too, never silently dropped.
	export interface PolicyWarning {
		policy_id: string | number | null;
		policy_title: string;
		applies_via: string | null;
		rule: string | null;
		artifact_id: string;
		matched: string[];
		errors?: string[];
	}
</script>

<script lang="ts">
	export let warnings: PolicyWarning[] = [];
	export let changeId = '';
	// An operator session approves here; it ticks this to approve over the
	// matches, and the approval carries `acknowledge_policy`.
	export let canAcknowledge = false;
	export let acknowledged = false;

	$: matched = warnings.filter((w) => w.matched.length > 0);
	$: broken = warnings.filter((w) => (w.errors ?? []).length > 0);
</script>

{#if matched.length || broken.length}
	<section class="card space-y-2 border-warn" data-testid="policy-warnings" aria-labelledby="policy-warnings-title">
		<h2 class="section-title" id="policy-warnings-title">
			{matched.length ? 'This change matches a policy' : 'A policy rule could not be read'}
		</h2>
		{#each matched as w}
			<div class="text-xs space-y-0.5" data-testid="policy-warning">
				<p class="text-warn">
					<span class="font-semibold">{w.policy_title || `Policy ${w.policy_id}`}</span>
					{#if w.applies_via === 'global'}(every host){/if} - {w.rule}
				</p>
				<ul class="list-disc pl-5 text-ink">
					{#each w.matched as what}
						<li><code>{what}</code>{#if w.artifact_id !== changeId} (step {w.artifact_id}){/if}</li>
					{/each}
				</ul>
			</div>
		{/each}
		{#if matched.length}
			{#if canAcknowledge}
				<label class="flex items-start gap-2 text-xs text-ink" data-testid="policy-ack">
					<input type="checkbox" bind:checked={acknowledged} />
					<span>I have read {matched.length === 1 ? 'this policy' : 'these policies'} and approve this change anyway. The acknowledgement is recorded with the approval.</span>
				</label>
			{/if}
			<p class="prose-note text-xs">
				Approving it needs the policy acknowledged. From a terminal:
				<code class="text-ink">hp changes approve {changeId} --acknowledge-policy</code>; an
				assistant passes <code class="text-ink">acknowledge_policy</code> with the approval code you
				relay.
			</p>
		{/if}
		{#each broken as w}
			<p class="text-xs text-muted" data-testid="policy-rule-error">
				{w.policy_title || `Policy ${w.policy_id}`}: {(w.errors ?? []).join('; ')} - this rule is not checked until it is fixed.
			</p>
		{/each}
	</section>
{/if}
