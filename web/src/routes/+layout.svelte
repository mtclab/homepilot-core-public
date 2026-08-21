<script lang="ts">
	import '../app.css';
	import { onMount, onDestroy } from 'svelte';
	import { goto, afterNavigate } from '$app/navigation';
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import { toast } from '$lib/stores';
	import { hasCookieSession, getToken, api, sessionStore, refreshSession, setToken } from '$lib/api';
	import { isAdmin as capIsAdmin } from '$lib/capabilities';
	import { startEventStream, stopEventStream } from '$lib/events';

	// `admin: true` items lead to admin-only endpoints (/agents/, /auth/tokens).
	// Showing them to a read-only session is a dead end — every button on the
	// page 403s. Settings deliberately stays open to everyone: it holds the API
	// base URL, the token field and health, which a read-only operator needs;
	// the admin-only Proxmox section inside it is gated there instead.
	const nav = [
		{ href: '/',          label: 'Overview' },
		{ href: '/artifacts', label: 'Artifacts' },
		{ href: '/review',    label: 'Review' },
		{ href: '/inventory', label: 'Inventory' },
		{ href: '/kb',        label: 'KB' },
		{ href: '/agents',    label: 'Agents',  admin: true },
		{ href: '/tokens',    label: 'Tokens',  admin: true },
		{ href: '/drift',     label: 'Drift' },
		{ href: '/tasks',     label: 'Tasks' },
		{ href: '/journal',   label: 'Journal' },
		{ href: '/settings',  label: 'Settings' },
	];

	// Default-deny while the session is still loading, so admin entries never
	// flash for a read-only operator.
	$: adminUser = capIsAdmin($sessionStore?.capabilities);
	$: visibleNav = nav.filter((item) => !item.admin || adminUser);

	$: current = $page.url.pathname;

	$: isLoginRoute = $page.url.pathname === base + '/login';

	// Derive the sidebar session straight from the reactive store so it tracks
	// login/logout without a full remount (the layout persists across the
	// login → app navigation, so a mount-only read went permanently stale).
	$: me = $sessionStore;
	let sessionLoading = true;

	function toLogin() {
		const returnTo = encodeURIComponent($page.url.pathname + $page.url.search);
		goto(`${base}/login?returnTo=${returnTo}`, { replaceState: true });
	}

	async function syncSession() {
		// Two credentials can carry a session: the HttpOnly cookie (normal path)
		// and the in-memory bearer token (the documented fallback when the cookie
		// cannot be set — e.g. a cross-origin API base). Bouncing on "no cookie"
		// alone dead-ended that fallback at the first navigation click, so check
		// BOTH; with neither, redirect without spending a request.
		const hasCredential = hasCookieSession() || getToken() !== '';
		if (!hasCredential && !isLoginRoute) {
			toLogin();
			return;
		}
		const me = await refreshSession();
		// A credential that the server rejects is no session at all.
		if (!me && !isLoginRoute) {
			sessionLoading = false;
			toLogin();
			return;
		}
		sessionLoading = false;
		// Open the shared SSE stream once a session exists so live artifact/drift
		// updates flow to every page. Idempotent — safe to call on each re-sync.
		if (me) startEventStream();
	}

	onMount(syncSession);
	onDestroy(stopEventStream);
	// Re-check after client-side navigations (e.g. login → app) so the session
	// panel updates. Skip the initial 'enter' — onMount already covers it.
	afterNavigate((nav) => {
		if (nav.type === 'enter') return;
		syncSession();
	});

	async function handleLogout() {
		if (typeof window !== 'undefined' && !window.confirm('Log out of this browser session?')) return;
		// Cookie-only sign-out: clears the session cookie server-side but does NOT
		// revoke the API token, so the same token still works for CLI/MCP and for
		// logging back in. Clear local state and return to the login screen.
		try { await api.logout(); } catch { /* ignore network errors */ }
		stopEventStream();
		setToken('');
		sessionStore.set(null);
		sessionLoading = false;
		goto(`${base}/login`);
	}

	function scopeBadge(scope: string | null | undefined): string {
		if (!scope) return 'text-muted';
		if (scope === '*' || scope === 'full') return 'text-ok';
		if (scope === 'admin') return 'text-warn';
		if (scope === 'read_only') return 'text-accent';
		return 'text-muted';
	}
</script>

{#if isLoginRoute}
	<slot />
{:else}
<div class="flex min-h-screen">
	<!-- Sidebar -->
	<nav class="w-44 shrink-0 bg-surface border-r border-border flex flex-col py-s-5">
		<div class="px-4 mb-6 flex items-center gap-2">
			<img src="{base}/logo.svg" alt="" class="w-6 h-6" />
			<span class="wordmark">HomePilot</span>
		</div>
		{#each visibleNav as { href, label }}
			<a
				href="{base}{href}"
				class="px-4 py-2 text-sm transition-colors
				       {(href === '/' ? current === base + '/' || current === base : current.startsWith(base + href) && href !== '/')
				         ? 'text-accent bg-raised border-l-2 border-accent'
				         : 'text-muted hover:text-ink hover:bg-raised'}"
			>
				{label}
			</a>
		{/each}

		<div class="mt-auto pt-4 px-4 border-t border-border mt-4">
			{#if me}
				<div class="flex items-center gap-2 mb-1">
					<span class="w-2 h-2 rounded-full bg-ok inline-block"></span>
					<span class="text-xs text-ink truncate">{me.token_label || me.prefix || 'session'}</span>
				</div>
				{#if me.scope || me.role}
					<span class="text-[10px] {scopeBadge(me.scope ?? me.role)}">
						{me.scope || me.role}
					</span>
				{/if}
				<button class="text-xs text-muted hover:text-danger transition-colors mt-1 block" on:click={handleLogout}>
					Log out
				</button>
			{:else if sessionLoading}
				<div class="flex items-center gap-2">
					<span class="w-2 h-2 rounded-full bg-warn inline-block animate-pulse"></span>
					<span class="text-xs text-muted">Loading…</span>
				</div>
			{:else}
				<div class="flex items-center gap-2">
					<span class="w-2 h-2 rounded-full bg-danger inline-block"></span>
					<span class="text-xs text-muted">No session</span>
				</div>
			{/if}
		</div>
	</nav>

	<!-- Main -->
	<main class="flex-1 overflow-auto p-6 flex flex-col">
		<div class="flex-1"><slot /></div>
		<!-- Maker's mark: quiet and muted, never louder than the utility row, and
		     never in the nav or a hero. -->
		<footer class="mt-s-6 pt-s-4 border-t border-divider flex justify-end">
			<a class="maker-mark" href="https://mtclab.net" target="_blank" rel="noopener">Built by MTC Lab</a>
		</footer>
	</main>
</div>
{/if}

<!-- Toast — the live region is always mounted (a region added at the same time
     as its text is not reliably announced) and errors are assertive. -->
<div
	class="fixed bottom-6 right-6 z-30"
	role={$toast?.kind === 'err' ? 'alert' : 'status'}
	aria-live={$toast?.kind === 'err' ? 'assertive' : 'polite'}
	aria-atomic="true"
>
	{#if $toast}
		<div
			class="px-4 py-3 rounded-md text-sm shadow-lg border
			       {$toast.kind === 'ok'
			         ? 'bg-ok-tint border-ok-border text-ok'
			         : 'bg-danger-tint border-danger-border text-danger'}"
		>
			{$toast.msg}
		</div>
	{/if}
</div>