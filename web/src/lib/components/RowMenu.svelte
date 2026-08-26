<script lang="ts">
	// The row overflow menu (#549 F1, principle 4 / P7 vocabulary).
	//
	// A table row earns ONE primary column and ONE actions affordance. Today the
	// fleet table spreads four to six buttons across every row, which is most of
	// why the owner called the console cluttered. This is where those collapse
	// to.
	//
	// F1 BUILDS it; adopting it page by page is F2-F5's job, because each
	// adoption is a behaviour change to a gated page (confirm flows, capability
	// gates) and not a token swap.
	//
	// Items are supplied by the caller through the slot and should carry
	// `role="menuitem"`; `let:close` closes the menu after one fires.

	/** Accessible name for the trigger, e.g. "Actions for web-01". */
	export let label = 'Row actions';
	/** Bindable so a parent can close every open menu when a page reloads. */
	export let open = false;

	let root: HTMLElement | undefined;
	let trigger: HTMLButtonElement | undefined;
	let menu: HTMLElement | undefined;

	export function close(): void {
		open = false;
	}

	function toggle(): void {
		open = !open;
	}

	/** Click anywhere that is not this menu shuts it — the standard dismissal. */
	function onWindowPointer(event: MouseEvent): void {
		if (!open) return;
		const target = event.target as Node | null;
		if (root && target && root.contains(target)) return;
		open = false;
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (!open || event.key !== 'Escape') return;
		open = false;
		// Escape must hand focus back to the trigger, or the operator is dropped
		// at the top of the document with no idea where they were.
		trigger?.focus();
	}

	function onTriggerKeydown(event: KeyboardEvent): void {
		if (event.key !== 'ArrowDown') return;
		event.preventDefault();
		open = true;
	}

	function focusFirstItem(node: HTMLElement): void {
		node.querySelector<HTMLElement>('[role="menuitem"], button, a')?.focus();
	}

	// Focus moves into the menu ONCE per opening: a re-render while the menu is
	// open must not yank focus back to the first item mid-keyboard-walk.
	let focusMoved = false;
	$: if (!open) focusMoved = false;
	$: if (open && menu && !focusMoved) {
		focusMoved = true;
		focusFirstItem(menu);
	}
</script>

<svelte:window on:click={onWindowPointer} on:keydown={onWindowKeydown} />

<div class="relative inline-block text-left" bind:this={root}>
	<button
		bind:this={trigger}
		type="button"
		class="btn btn-ghost btn-xs"
		aria-haspopup="menu"
		aria-expanded={open}
		aria-label={label}
		title={label}
		on:click|stopPropagation={toggle}
		on:keydown={onTriggerKeydown}>⋯</button
	>

	{#if open}
		<div
			bind:this={menu}
			role="menu"
			aria-label={label}
			class="card absolute right-0 z-20 mt-s-1 min-w-[10rem] p-s-1 flex flex-col items-stretch gap-s-1 shadow-lg"
		>
			<slot {close} />
		</div>
	{/if}
</div>
