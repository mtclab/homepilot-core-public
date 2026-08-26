<script lang="ts">
	// Test-only host for RowMenu: a menu needs items, and items come through a
	// slot. Kept as a real component (rather than a slot hack in the test) so
	// the fixture goes through the same compiler and a11y check as the console.
	import RowMenu from './RowMenu.svelte';
</script>

<button type="button" data-testid="outside">somewhere else</button>

<RowMenu label="Actions for web-01" let:close>
	<button type="button" role="menuitem" class="btn btn-ghost btn-xs" on:click={close}>Adopt</button>
	<button type="button" role="menuitem" class="btn btn-ghost btn-xs">Forget</button>
</RowMenu>
