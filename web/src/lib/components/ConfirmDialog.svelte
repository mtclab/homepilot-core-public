<script lang="ts">
	// The one confirm dialog for dangerous actions (UI plan 3.10, tiers of 5.3).
	// Changes (#805: destroy, forced stop, approve-and-apply from the keyboard)
	// and Hosts (#803: forced Stop, any verb on the self or a protected guest)
	// both use it; 3.10.0 unified the two lanes' dialogs into this one.
	//
	// It names the exact target and says the consequence in plain words - the
	// `consequence` text, the caller's slot for a richer body, or both. With
	// `confirmName` set it is the typed tier: the confirm button stays disabled
	// until that name is typed back (case aside, as the server compares it).
	// Without it, it is a plain "are you sure" with the verb on the button.
	//
	// A real <dialog> opened modally: focus is held inside and the page behind
	// is inert, Escape cancels, the confirm button is last and repeats the verb.
	import { createEventDispatcher, onMount, tick } from 'svelte';

	/** The heading: the verb and the exact target, e.g. "Stop web-01 (forced)". */
	export let title: string;
	/** What happens, in plain words, and what cannot be undone. */
	export let consequence = '';
	/** When set, the operator must type this to enable the confirm button. */
	export let confirmName: string | null = null;
	/** The confirm button's words; repeats the verb, never "OK". */
	export let confirmLabel = 'Confirm';
	/** Danger styling for the confirm button (off for e.g. a typed Start). */
	export let danger = true;
	/** The action is running: both buttons wait. */
	export let busy = false;

	const dispatch = createEventDispatcher<{ confirm: { typed: string }; cancel: void }>();

	let dialog: HTMLDialogElement | undefined;
	let input: HTMLInputElement | undefined;
	let confirmButton: HTMLButtonElement | undefined;
	let typed = '';
	const uid = `confirm-${Math.random().toString(36).slice(2, 9)}`;

	$: typedTier = confirmName != null && confirmName.trim() !== '';
	$: nameOk = !typedTier || typed.trim().toLowerCase() === (confirmName ?? '').trim().toLowerCase();

	onMount(async () => {
		// showModal traps focus and makes the page behind inert. A runtime
		// without it (the test DOM) still gets an open, labelled dialog.
		if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
		else dialog?.setAttribute('open', '');
		await tick();
		(input ?? confirmButton)?.focus();
	});

	function cancel() {
		if (busy) return;
		dispatch('cancel');
	}

	function onCancelEvent(event: Event) {
		// The browser's Escape: our cancel, not a silent close.
		event.preventDefault();
		cancel();
	}

	function onKeydown(event: KeyboardEvent) {
		// A runtime without showModal sends no `cancel` event on Escape.
		if (event.key === 'Escape') {
			event.preventDefault();
			cancel();
		}
	}

	function submit() {
		if (!nameOk || busy) return;
		dispatch('confirm', { typed: typed.trim() });
	}
</script>

<dialog
	bind:this={dialog}
	class="confirm-dialog card"
	aria-labelledby="{uid}-title"
	aria-describedby={consequence ? `${uid}-body` : undefined}
	data-testid="confirm-dialog"
	on:cancel={onCancelEvent}
	on:keydown={onKeydown}
>
	<form method="dialog" class="section-stack" on:submit|preventDefault={submit}>
		<h2 id="{uid}-title" class="text-sm font-semibold text-ink">{title}</h2>
		{#if consequence || $$slots.default}
			<div class="prose-body text-sm section-stack">
				{#if consequence}<p id="{uid}-body">{consequence}</p>{/if}
				<slot />
			</div>
		{/if}
		{#if typedTier}
			<label class="field-label block">
				Type <span class="font-mono text-ink">{confirmName}</span> to continue:
				<input
					bind:this={input}
					bind:value={typed}
					class="input mt-1 w-full font-mono text-sm"
					autocomplete="off"
					spellcheck="false"
					aria-label="Type {confirmName} to confirm"
				/>
			</label>
		{/if}
		<div class="flex justify-end gap-2">
			<button type="button" class="btn btn-ghost text-xs" disabled={busy} on:click={cancel}
				>Cancel</button
			>
			<button
				type="submit"
				bind:this={confirmButton}
				class="btn {danger ? 'btn-danger' : 'btn-primary'} text-xs"
				disabled={!nameOk || busy}>{busy ? 'Working...' : confirmLabel}</button
			>
		</div>
	</form>
</dialog>

<style>
	.confirm-dialog {
		width: min(30rem, calc(100vw - 2rem));
		padding: var(--space-5);
		color: var(--color-text);
	}
	.confirm-dialog::backdrop {
		background: rgb(0 0 0 / 0.55);
	}
</style>
