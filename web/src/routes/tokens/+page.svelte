<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, sessionStore, type TokenInfo } from '$lib/api';
	import { notify } from '$lib/stores';

	let tokens: TokenInfo[] = [];
	let total = 0;
	let loading = true;
	let isAdmin = false;
	let loadError = '';

	let showCreate = false;
	let newLabel = '';
	let newScope = 'full';
	let adminSecret = '';
	let createdToken = '';
	let creating = false;
	let revoking: string | null = null;

	// Prefix of the token backing the current session (once the server reports
	// it), used to warn on self-revoke.
	$: currentPrefix = $sessionStore?.prefix ?? '';

	async function load() {
		loading = true;
		loadError = '';
		try {
			const res = await api.listTokens();
			tokens = res.items;
			total = res.total;
			isAdmin = true;
		} catch (e) {
			// Only an actual 403 means "needs admin". A 500 (or anything else) is a
			// real error and must not masquerade as a permission problem — the old
			// `message.startsWith('403')` check could never be true (message is
			// humanized text), so every failure looked like a permission denial.
			if (e instanceof ApiError && e.status === 403) {
				isAdmin = false;
				notify('Admin scope required to manage tokens', 'err');
			} else {
				loadError = e instanceof Error ? e.message : String(e);
				notify(loadError, 'err');
			}
		} finally {
			loading = false;
		}
	}

	async function createToken() {
		if (!adminSecret.trim()) {
			notify('Admin secret is required', 'err');
			return;
		}
		creating = true;
		createdToken = '';
		try {
			const res = await api.createToken(newLabel.trim() || 'admin', newScope, adminSecret.trim());
			createdToken = res.token;
			notify('Token created — copy it now, it will not be shown again', 'ok');
			newLabel = '';
			newScope = 'full';
			adminSecret = '';
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			creating = false;
		}
	}

	async function revokeToken(prefix: string) {
		const isSelf = !!currentPrefix && prefix === currentPrefix;
		const msg = isSelf
			? `"${prefix}" backs your CURRENT session. Revoking it will log you out and break any CLI/MCP using it. Continue?`
			: `Revoke token "${prefix}"? Any CLI, MCP, or agent using it will immediately stop working.`;
		if (typeof window !== 'undefined' && !window.confirm(msg)) return;
		revoking = prefix;
		try {
			await api.revokeToken(prefix);
			notify('Token revoked', 'ok');
			await load();
		} catch (e) {
			notify(String(e), 'err');
		} finally {
			revoking = null;
		}
	}

	/** A token nobody can see the expiry of is a token that fails without warning. */
	function expiryLabel(expiresAt: string | null): string {
		if (!expiresAt) return 'never';
		const when = new Date(expiresAt);
		if (Number.isNaN(when.getTime())) return expiresAt;
		const days = Math.floor((when.getTime() - Date.now()) / 86_400_000);
		if (days < 0) return `expired ${when.toLocaleDateString()}`;
		if (days === 0) return 'today';
		return `in ${days} day${days === 1 ? '' : 's'}`;
	}

	function expiryClass(expiresAt: string | null): string {
		if (!expiresAt) return 'text-muted';
		const when = new Date(expiresAt);
		if (Number.isNaN(when.getTime())) return 'text-muted';
		const days = Math.floor((when.getTime() - Date.now()) / 86_400_000);
		if (days < 0) return 'text-danger';
		return days <= 7 ? 'text-warn' : 'text-muted';
	}

	function fmtDate(s: string | null): string {
		if (!s) return '—';
		return new Date(s).toLocaleString();
	}

	function scopeDisplay(scope: string | null, role: string | null): string {
		if (role) return role;
		if (scope === '*' || scope === 'full') return 'full';
		if (scope === 'read_only') return 'read_only';
		return scope || 'none';
	}

	onMount(load);
</script>

<div class="space-y-4">
	<div class="flex items-center justify-between">
		<h1 class="page-title">Tokens</h1>
		<div class="flex gap-2 items-center">
			<span class="text-muted text-xs">{total} tokens</span>
			{#if isAdmin}
				<button class="btn btn-primary text-xs" on:click={() => (showCreate = !showCreate)}>
					{showCreate ? 'Cancel' : '+ Create Token'}
				</button>
			{/if}
		</div>
	</div>

	{#if showCreate}
		<form class="card p-4 space-y-3" on:submit|preventDefault={createToken}>
			<h2 class="section-title">Create New Token</h2>

			<div class="flex gap-3 flex-wrap">
				<div class="flex-1 min-w-[160px]">
					<label class="field-label block mb-1">
						<span class="block mb-1">Label</span>
						<input
							class="input text-sm w-full"
							placeholder="e.g. ci-bot, admin"
							bind:value={newLabel}
						/>
					</label>
				</div>
				<div>
					<label class="field-label block mb-1">
						<span class="block mb-1">Scope</span>
						<select class="input text-sm" bind:value={newScope}>
							<option value="read_only">read_only</option>
							<option value="full">full</option>
							<option value="admin">admin</option>
						</select>
					</label>
				</div>
			</div>

			<div>
				<label class="field-label block mb-1">
					<span class="block mb-1">Admin Secret (HP_VAULT_PASSPHRASE)</span>
					<input
						type="password"
						class="input text-sm w-full font-mono"
						placeholder="Required to create tokens"
						bind:value={adminSecret}
						required
					/>
				</label>
				<p class="prose-note text-xs mt-1">Find this in your server's HP_VAULT_PASSPHRASE env var.</p>
			</div>

			<div class="flex justify-end">
				<button class="btn btn-primary text-xs" type="submit" disabled={creating}>
					{creating ? 'Creating…' : 'Create'}
				</button>
			</div>

			{#if createdToken}
				<div class="bg-canvas border border-ok-border rounded p-3">
					<p class="text-xs text-ok mb-1">New token (copy now):</p>
					<code class="text-xs text-ink break-all select-all">{createdToken}</code>
				</div>
			{/if}
		</form>
	{/if}

	{#if loading}
		<p class="text-muted text-sm">Loading…</p>
	{:else if loadError}
		<div class="card p-6 text-center space-y-3">
			<p class="text-danger">Could not load tokens.</p>
			<p class="text-xs text-muted">{loadError}</p>
			<button class="btn btn-ghost text-xs" on:click={load}>↻ Retry</button>
		</div>
	{:else if !isAdmin}
		<div class="card p-6 text-center">
			<p class="prose-note">You need admin scope to manage API tokens.</p>
		</div>
	{:else if tokens.length === 0}
		<p class="prose-note text-sm">No tokens found.</p>
	{:else}
		<div class="card overflow-x-auto">
			<table class="data-table text-xs">
				<thead>
					<tr>
						<th class="text-left">Prefix</th>
						<th class="text-left">Label</th>
						<th class="text-left">Scope</th>
						<th class="text-left">Role</th>
						<th class="text-left">Created</th>
						<th class="text-left">Last Used</th>
						<th class="text-left">Expires</th>
						<th class="text-left">Actions</th>
					</tr>
				</thead>
				<tbody>
					{#each tokens as t}
						<tr class="border-b border-divider">
							<td class="font-mono text-accent">
								{t.prefix}
								{#if currentPrefix && t.prefix === currentPrefix}
									<span class="ml-1 text-[10px] text-warn" title="This token backs your current session">(current)</span>
								{/if}
							</td>
							<td class="text-ink">{t.label || '—'}</td>
							<td>
								<span class="badge {t.scope === '*' || t.scope === 'full' ? 'badge-applied' : t.scope === 'read_only' ? 'badge-proposed' : 'badge-failed'}">
									{scopeDisplay(t.scope, t.role)}
								</span>
							</td>
							<td class="text-muted">{t.role || '—'}</td>
							<td class="text-muted">{fmtDate(t.created_at)}</td>
							<td class="text-muted">{fmtDate(t.last_used_at)}</td>
							<td class={expiryClass(t.expires_at)}>
								<!-- `expires_at` came back from the API and was rendered
								     nowhere, so a token that had stopped working looked
								     identical to one that had not (#435). -->
								{expiryLabel(t.expires_at)}
							</td>
							<td>
								<button
									class="btn btn-danger text-xs"
									disabled={revoking === t.prefix}
									on:click={() => revokeToken(t.prefix)}
								>
									{revoking === t.prefix ? 'Revoking…' : 'Revoke'}
								</button>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>