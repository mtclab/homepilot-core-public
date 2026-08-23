<script lang="ts">
	import { page } from '$app/stores';
	import Redirect from '$lib/Redirect.svelte';
</script>

<Redirect to="/changes/{$page.params.id}" />
