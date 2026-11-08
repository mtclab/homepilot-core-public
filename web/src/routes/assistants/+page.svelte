<script lang="ts">
	// Assistants (UI slice 1). What exists today is the token list: every
	// assistant, CLI and integration reaches HomePilot with a token, so the
	// tokens ARE the assistants for now - the panel Settings > Tokens rendered,
	// unchanged. The assistant registry the plan describes (a name and kind
	// per assistant, an allowed-tool list, an activity view) has no backend
	// yet, and this page says so rather than showing anything it cannot back.
	import TokensPanel from '$lib/components/TokensPanel.svelte';
	import Panel from '$lib/components/Panel.svelte';
</script>

<div class="page-stack">
	<h1 class="page-title">Assistants</h1>
	<p class="prose-note prose-measure text-sm" data-testid="assistants-not-built">
		Assistants reach HomePilot with a token, so for now this page is the token list.
		Naming each assistant, limiting the tools it may call and showing what it did
		are not built yet.
	</p>
	<div id="assistants-panel">
		<Panel>
			<TokensPanel />
		</Panel>
	</div>
</div>
