<script lang="ts">
	// The human-relay approval surface (#544), extracted so BOTH places that ask
	// an operator to approve can show it (#549 F4).
	//
	// It used to live only on the artifact detail page, so the review queue —
	// the screen an operator actually works through — could approve without ever
	// showing the code, and relaying one meant opening every artifact in turn.
	// The markup, the testids and the clear-lock behaviour are carried over
	// unchanged on purpose: the detail page's tests pin this panel.

	/** The per-artifact code. The panel renders nothing without one. */
	export let code: string | null | undefined = null;
	/** True once too many wrong codes were relayed over MCP. */
	export let locked = false;
	/** Clearing the lock is a write; a read-only session is not offered it. */
	export let canWrite = false;
	/** Disables the clear button while the caller has a request in flight. */
	export let busy = false;
	/** Called when the operator clears the lock. */
	export let onClear: () => void = () => {};
</script>

{#if code}
	<div class="card space-y-2" data-testid="approval-code">
		<h2 class="section-title">Approval code</h2>
		<p class="text-muted text-xs">
			Relay this code to the assistant to approve over MCP — it cannot read the code itself, so a
			valid code is proof you approved. Approving here needs no code.
		</p>
		<p class="font-mono text-lg text-ink tracking-wider select-all" data-testid="approval-code-value">
			{code}
		</p>
		{#if locked}
			<p class="text-danger text-xs" data-testid="approval-locked">
				Locked after too many wrong codes relayed over MCP. Clear the lock to allow coded approval
				again; the code above is unchanged.
			</p>
			{#if canWrite}
				<button class="btn btn-ghost text-xs" disabled={busy} on:click={onClear}
					>Clear approval lock</button
				>
			{/if}
		{/if}
	</div>
{/if}
