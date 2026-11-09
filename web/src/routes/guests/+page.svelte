<script lang="ts">
	// Guests (UI plan 2.7): friends' machines, invites and budgets, moved out
	// of Settings - they are operations, not configuration. The panel is the
	// one Settings > Guests rendered, unchanged; the guest NETWORK and the
	// provisioning defaults stay in Settings > Subsystems, where they are
	// configured.
	import GuestsPanel from '$lib/components/GuestsPanel.svelte';
	import Panel from '$lib/components/Panel.svelte';
</script>

<div class="page-stack">
	<h1 class="page-title">Guests</h1>
	<div id="guests-panel" class="max-w-3xl">
		<Panel title="Friends' machines">
			<GuestsPanel />
		</Panel>
	</div>
</div>
