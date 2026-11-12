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
	//
	// The panel is positioned `fixed` against the trigger (UI slice 5): the
	// fleet table sits in a scrolling box, and an absolutely placed menu on the
	// last rows was cut off by it - with the power verbs added, most of a
	// menu could hide below the box's edge. It opens upward when there is no
	// room below, and closes on a scroll or resize rather than drift away from
	// its row.
	import { createEventDispatcher } from 'svelte';

	/** Accessible name for the trigger, e.g. "Actions for web-01". */
	export let label = 'Row actions';
	/** Bindable so a parent can close every open menu when a page reloads. */
	export let open = false;
	/** The trigger's classes; the `trigger` slot replaces its content. */
	export let triggerClass = 'btn btn-ghost btn-xs';

	const dispatch = createEventDispatcher<{ open: void }>();

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
		// The event's path as it was at dispatch: an item that re-renders on
		// click (Start -> "Confirm start") is detached by the time this runs, and
		// `root.contains(target)` then read an inside click as an outside one.
		const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
		if (root && path.includes(root)) return;
		const target = event.target as Node | null;
		if (root && target && root.contains(target)) return;
		open = false;
	}

	// Where the panel goes, set each time it opens.
	let placement = '';
	const ROOM_BELOW = 280;

	function place(): void {
		if (!trigger || typeof window === 'undefined') return;
		const r = trigger.getBoundingClientRect();
		const right = Math.max(8, window.innerWidth - r.right);
		const below = window.innerHeight - r.bottom;
		placement =
			below < ROOM_BELOW && r.top > below
				? `bottom:${Math.round(window.innerHeight - r.top + 4)}px;right:${Math.round(right)}px;max-height:${Math.round(r.top - 12)}px`
				: `top:${Math.round(r.bottom + 4)}px;right:${Math.round(right)}px;max-height:${Math.round(below - 12)}px`;
	}

	/** A scroll anywhere but inside the menu moves the row away: close. */
	function onWindowScroll(event: Event): void {
		if (!open) return;
		const target = event.target as Node | null;
		if (menu && target && menu.contains(target)) return;
		open = false;
	}

	function onWindowResize(): void {
		if (open) open = false;
	}

	let wasOpen = false;
	$: if (open !== wasOpen) {
		wasOpen = open;
		if (open) {
			place();
			dispatch('open');
		}
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
		node
			.querySelector<HTMLElement>(
				'[role="menuitem"]:not([disabled]), button:not([disabled]), a',
			)
			?.focus();
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

<svelte:window
	on:click={onWindowPointer}
	on:keydown={onWindowKeydown}
	on:scroll|capture={onWindowScroll}
	on:resize={onWindowResize}
/>

<div class="relative inline-block text-left" bind:this={root}>
	<button
		bind:this={trigger}
		type="button"
		class={triggerClass}
		aria-haspopup="menu"
		aria-expanded={open}
		aria-label={label}
		title={label}
		on:click|stopPropagation={toggle}
		on:keydown={onTriggerKeydown}><slot name="trigger">⋯</slot></button
	>

	{#if open}
		<div
			bind:this={menu}
			role="menu"
			aria-label={label}
			class="card row-menu fixed z-30 min-w-[12rem] max-w-[20rem] overflow-y-auto p-s-1 flex flex-col items-stretch shadow-lg"
			style={placement}
		>
			<slot {close} />
		</div>
	{/if}
</div>

<style>
	/* Menu items read down one left edge, whatever element they are, as ONE
	   list: no box around each item and no gap between them, the row under the
	   pointer or focus lit (3.10.0 walk: in Day each item read as a separate
	   outlined button). A filled item - an armed "Confirm start" - keeps its
	   fill, so the one action waiting for you stands out of the list. */
	.row-menu :global(.btn) {
		justify-content: flex-start;
		text-align: left;
		width: 100%;
		border-color: transparent;
		border-radius: var(--radius-sm, 0.25rem);
	}
	.row-menu :global(.btn-ghost:hover:not(:disabled)) {
		border-color: transparent;
		background: var(--color-surface-raised);
	}
</style>
