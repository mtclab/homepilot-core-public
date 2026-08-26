<script lang="ts">
	import Redirect from '$lib/Redirect.svelte';
</script>

<!-- Settings is tabbed since #549 F6, so the token panel is a sector, not the
     top of a page: land on it rather than on the API-base form. -->
<Redirect to="/settings?tab=tokens" />
