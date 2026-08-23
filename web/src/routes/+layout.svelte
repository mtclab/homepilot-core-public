<script lang="ts">
	import '../app.css';
	import { onMount, onDestroy } from 'svelte';
	import { goto, afterNavigate } from '$app/navigation';
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import { toast } from '$lib/stores';
	import { hasCookieSession, getToken, api, sessionStore, refreshSession, setToken } from '$lib/api';
	
	import { startEventStream, stopEventStream, streamConnected } from '$lib/events';

	// `admin: true` items lead to admin-only endpoints (/agents/, /auth/tokens).
	// Showing them to a read-only session is a dead end — every button on the
	// page 403s. Settings deliberately stays open to everyone: it holds the API
	// base URL, the token field and health, which a read-only operator needs;
	// the admin-only Proxmox section inside it is gated there instead.
	// Five groups (#514 S4, decided 2026-08-23): glance / my machines / what
	// HomePilot does / what happened / plumbing. Eleven flat tabs mixed four
	// different jobs; Changes and Records carry their views as in-page tabs.
	const nav = [
		{ href: '/',         label: 'Overview' },
		{ href: '/hosts',    label: 'Hosts' },
		{ href: '/changes',  label: 'Changes' },
		{ href: '/records',  label: 'Records' },
		{ href: '/settings', label: 'Settings' },
	];

	// No admin-gated entries remain: the admin-only surfaces (tokens, alert
	// rules, fleet credentials) live inside Settings and Hosts, which gate
	// their own controls.
	$: visibleNav = nav;

	$: current = $page.url.pathname;

	$: isLoginRoute = $page.url.pathname === base + '/login';

	// Derive the sidebar session straight from the reactive store so it tracks
	// login/logout without a full remount (the layout persists across the
	// login → app navigation, so a mount-only read went permanently stale).
	$: me = $sessionStore;
	let sessionLoading = true;
	// Mobile nav state. Closed by default and closed again on navigation - a
	// drawer left open over the page a tap just loaded is its own dead end.
	let navOpen = false;

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
		// However the navigation happened - a link, the back button, a redirect -
		// the drawer must not be left open over the page it just loaded.
		navOpen = false;
		if (nav.type === 'enter') return;
		syncSession();
	});

	// Escape closes the drawer. It is the one overlay in the UI, and an overlay
	// a keyboard user cannot dismiss is a trap.
	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && navOpen) navOpen = false;
	}

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

<svelte:window on:keydown={onKeydown} />

{#if isLoginRoute}
	<slot />
{:else}
<div class="flex flex-col md:flex-row min-h-screen">
	<!-- Mobile bar: the sidebar is a fixed 176px column, which on a phone left
	     almost nothing for the tables that ARE the product (#445 B6). Below md
	     the nav collapses behind this bar instead. -->
	<div class="md:hidden flex items-center gap-3 px-4 py-3 bg-surface border-b border-border">
		<button
			class="btn btn-ghost text-xs"
			aria-expanded={navOpen}
			aria-controls="main-nav"
			on:click={() => (navOpen = !navOpen)}
		>
			<span class="sr-only">{navOpen ? 'Close navigation' : 'Open navigation'}</span>
			<span aria-hidden="true">{navOpen ? '✕' : '☰'}</span>
		</button>
		<img src="{base}/logo.svg" alt="" class="w-5 h-5" />
		<span class="wordmark">HomePilot</span>
	</div>

	<!-- Sidebar -->
	<nav
		id="main-nav"
		class="w-full md:w-44 shrink-0 bg-surface border-b md:border-b-0 md:border-r border-border
		       flex-col py-s-5 {navOpen ? 'flex' : 'hidden'} md:flex"
	>
		<div class="px-4 mb-6 hidden md:flex items-center gap-2">
			<img src="{base}/logo.svg" alt="" class="w-6 h-6" />
			<span class="wordmark">HomePilot</span>
		</div>
		{#each visibleNav as { href, label }}
			<a
				href="{base}{href}"
				on:click={() => (navOpen = false)}
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
			{#if me && !$streamConnected}
				<!-- The stream reconnects silently with backoff, so when it is down
				     the UI just stops updating. "Nothing is happening" and "I am not
				     being told what is happening" are opposite conclusions on an ops
				     console (#435). -->
				<div class="flex items-center gap-2 mt-2" title="Reconnecting to the live update stream">
					<span class="w-2 h-2 rounded-full bg-warn inline-block animate-pulse"></span>
					<span class="text-[10px] text-warn">Live updates offline</span>
				</div>
			{/if}
		</div>
	</nav>

	<!-- Main -->
	<!-- `min-w-0` is what actually lets the wide tables scroll inside their own
	     container: without it a flex child refuses to shrink below its content
	     and the whole PAGE scrolls sideways instead. -->
	<main class="flex-1 min-w-0 overflow-auto p-4 md:p-6 flex flex-col">
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