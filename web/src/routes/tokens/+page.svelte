<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type TokenInfo } from '$lib/api';
	import { notify } from '$lib/stores';

	let tokens: TokenInfo[] = [];
	let total = 0;
	let loading = true;
	let isAdmin = false;

	let showCreate = false;
	let newLabel = '';
	let newScope = 'full';
	let adminSecret = '';
	let createdToken = '';
	let creating = false;
	let revoking: string | null = null;

	async function load() {
		loading = true;
		try {
			const res = await api.listTokens();
			tokens = res.items;
			total = res.total;
			isAdmin = true;
		} catch (e: any) {
			if (e?.message?.startsWith('403')) {
				isAdmin = false;
				notify('Admin scope required to manage tokens', 'err');
			} else {
				notify(String(e), 'err');
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
		<h1 class="text-lg font-bold text-slate-100">Tokens</h1>
		<div class="flex gap-2 items-center">
			<span class="text-slate-500 text-xs">{total} tokens</span>
			{#if isAdmin}
				<button class="btn btn-primary text-xs" on:click={() => (showCreate = !showCreate)}>
					{showCreate ? 'Cancel' : '+ Create Token'}
				</button>
			{/if}
		</div>
	</div>

	{#if showCreate}
		<form class="card p-4 space-y-3" on:submit|preventDefault={createToken}>
			<h2 class="text-sm font-semibold text-slate-300">Create New Token</h2>

			<div class="flex gap-3 flex-wrap">
				<div class="flex-1 min-w-[160px]">
					<label class="block text-xs text-slate-400 mb-1">Label</label>
					<input
						class="input text-sm w-full"
						placeholder="e.g. ci-bot, admin"
						bind:value={newLabel}
					/>
				</div>
				<div>
					<label class="block text-xs text-slate-400 mb-1">Scope</label>
					<select class="input text-sm" bind:value={newScope}>
						<option value="read_only">read_only</option>
						<option value="full">full</option>
						<option value="admin">admin</option>
					</select>
				</div>
			</div>

			<div>
				<label class="block text-xs text-slate-400 mb-1">Admin Secret (HP_VAULT_PASSPHRASE)</label>
				<input
					type="password"
					class="input text-sm w-full font-mono"
					placeholder="Required to create tokens"
					bind:value={adminSecret}
					required
				/>
				<p class="text-xs text-slate-600 mt-1">Find this in your server's HP_VAULT_PASSPHRASE env var.</p>
			</div>

			<div class="flex justify-end">
				<button class="btn btn-primary text-xs" type="submit" disabled={creating}>
					{creating ? 'Creating…' : 'Create'}
				</button>
			</div>

			{#if createdToken}
				<div class="bg-slate-900 border border-emerald-700 rounded p-3">
					<p class="text-xs text-emerald-400 font-mono mb-1">New token (copy now):</p>
					<code class="text-xs text-slate-200 break-all select-all">{createdToken}</code>
				</div>
			{/if}
		</form>
	{/if}

	{#if loading}
		<p class="text-slate-500 text-sm">Loading…</p>
	{:else if !isAdmin}
		<div class="card p-6 text-center">
			<p class="text-slate-400">You need admin scope to manage API tokens.</p>
		</div>
	{:else if tokens.length === 0}
		<p class="text-slate-500 text-sm">No tokens found.</p>
	{:else}
		<div class="card overflow-x-auto">
			<table class="w-full text-xs">
				<thead>
					<tr class="text-slate-400 border-b border-slate-700">
						<th class="text-left pb-2 pr-4">Prefix</th>
						<th class="text-left pb-2 pr-4">Label</th>
						<th class="text-left pb-2 pr-4">Scope</th>
						<th class="text-left pb-2 pr-4">Role</th>
						<th class="text-left pb-2 pr-4">Created</th>
						<th class="text-left pb-2 pr-4">Last Used</th>
						<th class="text-left pb-2">Actions</th>
					</tr>
				</thead>
				<tbody>
					{#each tokens as t}
						<tr class="border-b border-slate-700/50">
							<td class="py-2 pr-4 font-mono text-sky-400">{t.prefix}</td>
							<td class="py-2 pr-4 text-slate-300">{t.label || '—'}</td>
							<td class="py-2 pr-4">
								<span class="badge {t.scope === '*' || t.scope === 'full' ? 'badge-applied' : t.scope === 'read_only' ? 'badge-proposed' : 'badge-failed'}">
									{scopeDisplay(t.scope, t.role)}
								</span>
							</td>
							<td class="py-2 pr-4 text-slate-400">{t.role || '—'}</td>
							<td class="py-2 pr-4 text-slate-500">{fmtDate(t.created_at)}</td>
							<td class="py-2 pr-4 text-slate-600">{fmtDate(t.last_used_at)}</td>
							<td class="py-2">
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