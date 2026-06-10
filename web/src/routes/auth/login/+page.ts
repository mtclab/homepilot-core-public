import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

// Canonical UI login lives at /ui/login. /ui/auth/login is a legacy path that
// some older links/docs reference; redirect it to the canonical route instead
// of 404-bouncing through the auth guard (bugs-found.md Bug 3).
export const ssr = false;
export const prerender = false;

export function load({ url }: { url: URL }) {
	const returnTo = url.searchParams.get('returnTo');
	const target = `${base}/login${returnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : ''}`;
	throw redirect(308, target);
}
