<script lang="ts">
	import Redirect from '$lib/Redirect.svelte';
</script>

<!-- The agent chores are a Hosts view now (owner decision 2); /agents was
     the old Agents tab, so it lands there rather than on the fleet list. -->
<Redirect to="/hosts/agents" />
