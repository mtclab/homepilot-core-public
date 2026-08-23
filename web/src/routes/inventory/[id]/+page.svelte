<script lang="ts">
	import { page } from '$app/stores';
	import Redirect from '$lib/Redirect.svelte';
</script>

<Redirect to="/hosts/{$page.params.id}" />
