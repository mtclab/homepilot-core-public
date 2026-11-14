<script lang="ts">
	// One friend on the Guests page (3.12): who they are, the machines they hold
	// with their live state and a way to each host page, how much of their
	// budget is used, the two things the operator does (Invite, Budget), and
	// the invite history folded underneath. On a desk it is one grid row; on a
	// phone the same markup stacks into a card (see the media query below).
	import { createEventDispatcher } from 'svelte';
	import { base } from '$app/paths';
	import type { Friend, FriendInvite } from '$lib/api';
	import Lamp from '$lib/components/Lamp.svelte';
	import Tag from '$lib/components/Tag.svelte';
	import {
		budgetBars,
		earlierSummary,
		foldedInvites,
		inviteWords,
		machineTone,
		sizeLine,
		usedUpSentence,
		AXIS_WORD,
	} from '$lib/guests';
	import { exactTime } from '$lib/relativeTime';

	export let friend: Friend;
	/** The clock the history is worded against; injectable for tests. */
	export let now: Date = new Date();

	const dispatch = createEventDispatcher<{
		invite: { cn: string };
		budget: { cn: string };
		revoke: { cn: string; invite: FriendInvite };
	}>();

	let showAll = false;
	const uid = `friend-${Math.random().toString(36).slice(2, 9)}`;

	$: budget = budgetBars(friend);
	$: fullSentence = usedUpSentence(friend);
	$: folded = foldedInvites(friend.invites);
	$: listed = showAll ? friend.invites : folded.shown;
	$: hadMachine = friend.invites.some((i) => i.state === 'redeemed');
</script>

<li class="friend" data-friend={friend.cn} aria-labelledby="{uid}-name">
	<div class="who">
		<b id="{uid}-name">{friend.cn}</b>
		<span>certificate name</span>
	</div>

	<div class="machines">
		{#each friend.machines as m (m.host_id)}
			{@const tone = machineTone(m.state)}
			<div class="machine" data-machine={m.name}>
				<Lamp state={tone.lamp} />
				<a class="machine-link" href="{base}/hosts/{m.host_id}">{m.name}</a>
				<Tag tone={tone.tag}>{m.state}</Tag>
				<Tag>{sizeLine(m)}</Tag>
			</div>
		{:else}
			<span class="none">{hadMachine ? 'No machine now' : 'No machine yet'}</span>
		{/each}
	</div>

	<div class="budget" aria-label="Budget for {friend.cn}">
		{#each budget.bars as bar (bar.axis)}
			<span>{bar.word}</span>
			<span
				class="bar"
				role="meter"
				aria-label="{bar.word} used"
				aria-valuemin={0}
				aria-valuemax={100}
				aria-valuenow={bar.pct}
				aria-valuetext={bar.text}
				><i class:full={bar.full} style="width: {bar.pct}%"></i></span
			>
			<span class="r" data-budget-axis={bar.axis}>{bar.text}</span>
		{/each}
		{#if !friend.limits}
			<span class="free">No budget: nothing caps what they hold</span>
		{:else if budget.unlimited.length}
			<span class="free">no limit on {budget.unlimited.map((a) => AXIS_WORD[a]).join(', ')}</span>
		{/if}
	</div>

	<div class="acts">
		<button type="button" class="btn" aria-label="Invite {friend.cn}" on:click={() => dispatch('invite', { cn: friend.cn })}
			>Invite</button
		>
		<button type="button" class="btn" aria-label="Budget for {friend.cn}" on:click={() => dispatch('budget', { cn: friend.cn })}
			>Budget</button
		>
	</div>

	<div class="hist">
		{#if fullSentence}
			<p class="used-up" data-used-up>{fullSentence}</p>
		{/if}
		{#if friend.invites.length}
			<ul class="invites" aria-label="Invites for {friend.cn}">
				{#each listed as inv (inv.id)}
					{@const words = inviteWords(inv, now)}
					<li data-invite={inv.prefix} data-state={inv.state}>
						<span>Invite</span>
						<time datetime={words.at ?? undefined} title={exactTime(words.at)}>{words.text}</time>
						<code>{inv.prefix}</code>
						{#if showAll}<span class="size">{sizeLine(inv.caps)}</span>{/if}
						{#if inv.state === 'open'}
							<button
								type="button"
								class="link danger"
								aria-label="Revoke invite {inv.prefix}"
								on:click={() => dispatch('revoke', { cn: friend.cn, invite: inv })}>Revoke</button
							>
						{/if}
					</li>
				{/each}
			</ul>
			{#if folded.rest.length}
				<p class="more">
					{#if !showAll}<span>{earlierSummary(folded.rest)}</span>{/if}
					<button type="button" class="link" aria-expanded={showAll} on:click={() => (showAll = !showAll)}
						>{showAll ? 'show fewer' : 'show all'}</button
					>
				</p>
			{/if}
		{:else}
			<p class="more">No invites yet.</p>
		{/if}
	</div>
</li>

<style>
	.friend {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1.6fr) minmax(0, 1.3fr) auto;
		gap: var(--space-2) var(--space-4);
		align-items: start;
		padding: var(--space-3);
		border-top: 1px solid var(--color-divider);
	}
	.friend:first-child {
		border-top: 0;
	}
	.who {
		min-width: 0;
	}
	.who b {
		display: block;
		font-family: var(--font-serif);
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--color-text-strong);
		overflow-wrap: anywhere;
	}
	.who span {
		font-family: var(--font-sans);
		font-size: 0.6875rem;
		color: var(--color-muted);
	}
	.machines {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 0;
	}
	.machine {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2);
		min-width: 0;
	}
	.machine-link {
		font-family: var(--font-mono);
		font-size: 0.78125rem;
		color: var(--color-accent);
		overflow-wrap: anywhere;
	}
	.machine-link:hover {
		text-decoration: underline;
	}
	.none {
		font-family: var(--font-serif);
		font-size: 0.8125rem;
		color: var(--color-muted);
	}
	.budget {
		display: grid;
		/* Fixed label and figure columns, so every friend's bars start and end
		   at the same place down the list. */
		grid-template-columns: 4.25rem minmax(2.5rem, 1fr) 5.75rem;
		gap: 0.1875rem var(--space-2);
		align-items: center;
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--color-muted);
		min-width: 0;
	}
	.bar {
		display: block;
		height: 6px;
		background: var(--color-surface-raised);
		border: 1px solid var(--color-border);
		border-radius: 1px;
		overflow: hidden;
	}
	.bar i {
		display: block;
		height: 100%;
		background: var(--color-accent);
	}
	.bar i.full {
		background: var(--color-warn);
	}
	.r {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--color-text);
	}
	.free {
		grid-column: 1 / -1;
		font-family: var(--font-sans);
	}
	.acts {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		justify-content: flex-end;
	}
	.hist {
		grid-column: 1 / -1;
		display: flex;
		flex-direction: column;
		gap: 0.1875rem;
		padding-top: var(--space-2);
		border-top: 1px dashed var(--color-divider);
		font-size: 0.75rem;
		color: var(--color-muted);
	}
	.used-up {
		font-family: var(--font-serif);
		font-size: 0.8125rem;
		color: var(--color-warn);
	}
	.invites {
		display: flex;
		flex-direction: column;
		gap: 0.1875rem;
	}
	.invites li {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.25rem var(--space-2);
	}
	.invites time {
		color: var(--color-text);
	}
	.invites code {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--color-text);
		overflow-wrap: anywhere;
	}
	.size {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
	}
	.more {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}
	.link {
		font-family: var(--font-sans);
		font-size: 0.75rem;
		color: var(--color-accent);
		background: none;
		border: 0;
		padding: 0;
		cursor: pointer;
	}
	.link:hover {
		text-decoration: underline;
	}
	.link.danger {
		color: var(--color-danger);
	}
	/* Phone: the row becomes a card - every part on its own line, the actions
	   wrapping under the budget, nothing wider than the screen. */
	@media (max-width: 767px) {
		.friend {
			grid-template-columns: minmax(0, 1fr);
			gap: var(--space-2);
			margin: var(--space-2);
			border: 1px solid var(--color-border);
			border-radius: var(--radius-md);
			background: var(--color-bg);
		}
		.friend:first-child {
			border-top: 1px solid var(--color-border);
		}
		.acts {
			justify-content: flex-start;
		}
	}
</style>
