<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import { toast } from '$lib/stores';
	import { hasCookieSession, api, sessionStore, refreshSession, type MeInfo } from '$lib/api';

	const nav = [
		{ href: '/',          label: 'Overview' },
		{ href: '/artifacts', label: 'Artifacts' },
		{ href: '/review',    label: 'Review' },
		{ href: '/inventory', label: 'Inventory' },
		{ href: '/kb',        label: 'KB' },
		{ href: '/agents',    label: 'Agents' },
		{ href: '/tokens',    label: 'Tokens' },
		{ href: '/drift',     label: 'Drift' },
		{ href: '/journal',   label: 'Journal' },
		{ href: '/settings',  label: 'Settings' },
	];

	$: current = $page.url.pathname;

	$: isLoginRoute = $page.url.pathname === base + '/login';

	let me: MeInfo | null = null;
	let sessionLoading = true;

	onMount(async () => {
		if (!hasCookieSession() && !isLoginRoute) {
			const returnTo = encodeURIComponent($page.url.pathname + $page.url.search);
			goto(`${base}/login?returnTo=${returnTo}`, { replaceState: true });
			return;
		}
		me = await refreshSession();
		sessionLoading = false;
	});

	async function handleLogout() {
		try { await api.logout(); } catch { /* ignore */ }
		sessionStore.set(null);
		me = null;
		goto(`${base}/login`);
	}

	function scopeBadge(scope: string | null | undefined): string {
		if (!scope) return 'text-slate-500';
		if (scope === '*' || scope === 'full') return 'text-emerald-400';
		if (scope === 'admin') return 'text-amber-400';
		if (scope === 'read_only') return 'text-sky-400';
		return 'text-slate-400';
	}
</script>

{#if isLoginRoute}
	<slot />
{:else}
<div class="flex min-h-screen">
	<!-- Sidebar -->
	<nav class="w-44 shrink-0 bg-slate-900 border-r border-slate-700 flex flex-col py-4">
		<div class="px-4 mb-6 flex items-center gap-2">
			<img src="{base}/logo.svg" alt="" class="w-6 h-6" />
			<span class="text-sky-400 font-bold text-sm tracking-widest">HOMEPILOT</span>
		</div>
		{#each nav as { href, label }}
			<a
				href="{base}{href}"
				class="px-4 py-2 text-sm transition-colors
				       {(href === '/' ? current === base + '/' || current === base : current.startsWith(base + href) && href !== '/')
				         ? 'text-sky-400 bg-slate-800 border-l-2 border-sky-400'
				         : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'}"
			>
				{label}
			</a>
		{/each}

		<div class="mt-auto pt-4 px-4 border-t border-slate-700 mt-4">
			{#if me}
				<div class="flex items-center gap-2 mb-1">
					<span class="w-2 h-2 rounded-full bg-emerald-400 inline-block"></span>
					<span class="text-xs text-slate-300 font-mono truncate">{me.token_label || me.prefix}</span>
				</div>
				{#if me.scope || me.role}
					<span class="text-[10px] font-mono {scopeBadge(me.scope ?? me.role)}">
						{me.scope || me.role}
					</span>
				{/if}
				<button class="text-xs text-slate-500 hover:text-red-400 transition-colors mt-1 block" on:click={handleLogout}>
					Log out
				</button>
			{:else if sessionLoading}
				<div class="flex items-center gap-2">
					<span class="w-2 h-2 rounded-full bg-yellow-400 inline-block animate-pulse"></span>
					<span class="text-xs text-slate-500">Loading…</span>
				</div>
			{:else}
				<div class="flex items-center gap-2">
					<span class="w-2 h-2 rounded-full bg-red-400 inline-block"></span>
					<span class="text-xs text-slate-500">No session</span>
				</div>
			{/if}
		</div>
	</nav>

	<!-- Main -->
	<main class="flex-1 overflow-auto p-6">
		<slot />
	</main>
</div>
{/if}

<!-- Toast -->
{#if $toast}
	<div
		class="fixed bottom-6 right-6 px-4 py-3 rounded-lg text-sm font-mono shadow-lg border
		       {$toast.kind === 'ok'
		         ? 'bg-emerald-900 border-emerald-700 text-emerald-200'
		         : 'bg-red-900 border-red-700 text-red-200'}"
	>
		{$toast.msg}
	</div>
{/if}