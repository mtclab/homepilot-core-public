<script lang="ts">
	// Test-only host for RowMenu: a menu needs items, and items come through a
	// slot. Kept as a real component (rather than a slot hack in the test) so
	// the fixture goes through the same compiler and a11y check as the console.
	import { flushSync } from 'svelte';
	import RowMenu from './RowMenu.svelte';

	// An item that turns into its own confirm on click, the way the Hosts power
	// verbs do: the clicked element is gone from the DOM once the click lands.
	// A real browser runs a microtask checkpoint between listeners of a user
	// click, so Svelte has re-rendered before the window listener runs; a
	// scripted click in the test DOM does not, so the fixture flushes itself.
	let armed = false;
	function arm() {
		armed = true;
		flushSync();
	}
</script>

<button type="button" data-testid="outside">somewhere else</button>

<RowMenu label="Actions for web-01" let:close>
	<button type="button" role="menuitem" class="btn btn-ghost btn-xs" on:click={close}>Adopt</button>
	<button type="button" role="menuitem" class="btn btn-ghost btn-xs">Forget</button>
	{#if armed}
		<button type="button" role="menuitem" class="btn btn-ghost btn-xs" on:click={close}>Confirm arm</button>
	{:else}
		<button type="button" role="menuitem" class="btn btn-ghost btn-xs" on:click={arm}>Arm</button>
	{/if}
</RowMenu>
