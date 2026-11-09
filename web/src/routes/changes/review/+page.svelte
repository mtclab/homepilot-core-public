<script lang="ts">
	// The review queue became the Inbox (UI_PLAN 2.2): same queue, plus the
	// approved changes still to run. Old links and bookmarks land there.
	import Redirect from '$lib/Redirect.svelte';
</script>

<Redirect to="/changes/inbox" />
