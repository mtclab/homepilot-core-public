<script lang="ts">
	// Hosts > Agents (owner decision 2): the join requests, the bootstrap
	// one-liner and the hub token, moved off the top of the fleet list
	// unchanged. The components own their reads and their empty states.
	import { sessionStore } from '$lib/api';
	import { canWrite as capCanWrite } from '$lib/capabilities';
	import EnrollAgent from '$lib/components/EnrollAgent.svelte';
	import JoinRequests from '$lib/components/JoinRequests.svelte';
	import Icon from '$lib/components/Icon.svelte';

	let enrollShow: 'bootstrap' | 'hub' | null = null;
	let joinRequests: JoinRequests | null = null;
	$: canWrite = capCanWrite($sessionStore?.capabilities);
</script>

<div class="page-stack">
	<div class="flex flex-wrap items-center justify-between gap-s-2">
		<h1 class="page-title">Agents</h1>
		<div class="flex gap-2">
			<button class="btn btn-ghost" on:click={() => joinRequests?.load()}>
				<Icon name="refresh" />Reload
			</button>
			{#if canWrite}
				<button class="btn btn-primary"
					on:click={() => (enrollShow = enrollShow === 'bootstrap' ? null : 'bootstrap')}>
					{enrollShow === 'bootstrap' ? 'Cancel' : 'Enroll agent'}
				</button>
				<button class="btn btn-ghost"
					on:click={() => (enrollShow = enrollShow === 'hub' ? null : 'hub')}>
					{enrollShow === 'hub' ? 'Hide' : 'Hub token'}
				</button>
			{/if}
		</div>
	</div>

	<p class="prose-note prose-measure text-xs">
		Hosts knocking on the agent hub, bootstrap tokens nobody has used yet, and the
		enrolment window. When nobody is waiting, only the enrol controls above apply.
	</p>

	<JoinRequests
		bind:this={joinRequests}
		{canWrite}
		on:enrolWithBootstrap={() => (enrollShow = 'bootstrap')}
	/>

	<EnrollAgent show={enrollShow} />
</div>
