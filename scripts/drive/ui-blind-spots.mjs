/**
 * Drive the REAL operator UI and prove tranche 7's four fixes on the shipped
 * artifact (#648).
 *
 * The suite proves these against the Svelte components. This proves them
 * against the built app, in a browser, talking to a running control plane -
 * which is the half the epic says found every defect the green suite missed.
 *
 * The failing reads are produced by ABORTING them in the browser rather than
 * by breaking the backend: it is the same condition the code branches on (a
 * rejected fetch), it needs no write to a live instance, and it cannot leave
 * the control plane in a state somebody has to clean up.
 *
 * Run it on the staging box, never on the workspace:
 *   printf '%s' "$TOKEN" | ssh bilvi-dev-stage@10.96.16.18 \
 *     'BASE=http://10.96.16.100:8000 node /tmp/ui-blind-spots.mjs'
 */
import { chromium } from 'playwright';
import { readFileSync } from 'node:fs';

const BASE = process.env.BASE || 'http://10.96.16.100:8000';
const TOKEN = readFileSync(0, 'utf8').trim();
if (!TOKEN) {
	console.error('FAIL: no token on stdin');
	process.exit(2);
}

const results = [];
function check(name, ok, detail = '') {
	results.push({ name, ok, detail });
	console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` -- ${detail}` : ''}`);
}

/** Abort every request whose URL matches, so the page sees a rejected fetch. */
async function breakRead(page, pattern) {
	await page.route(pattern, (route) => route.abort('failed'));
}

const browser = await chromium.launch();
const ctx = await browser.newContext({ baseURL: BASE, ignoreHTTPSErrors: true });
const page = await ctx.newPage();

// Log in the way an operator does: paste the token into Settings and save.
await page.goto('/ui/login');
await page.waitForLoadState('networkidle');
// The token form is the second pane on /ui/login; its control is "Connect".
await page.locator('#login-token').fill(TOKEN);
await page.getByRole('button', { name: 'Connect' }).click();
// The URL is not the signal - the app can hold /ui/login in the bar while the
// shell is rendered. Wait for the thing only a logged-in shell has, rather
// than sampling the body at whatever moment networkidle happened to fire.
let loggedIn = true;
try {
	await page.getByRole('button', { name: /log out/i }).waitFor({ timeout: 15000 });
} catch {
	loggedIn = false;
}
check('logged in', loggedIn, page.url());

// ── 1. Overview: a rejected side call must not read as a calm estate ────────
await breakRead(page, '**/tasks*');
await page.goto('/ui/');
await page.waitForLoadState('networkidle');
let body = await page.locator('body').innerText();
check(
	'Overview names the unread source when the task read fails',
	/could not read recent tasks/i.test(body),
	body.slice(0, 160).replace(/\s+/g, ' '),
);
check('Overview does NOT claim nothing needs you', !/nothing needs you/i.test(body));
check(
	'the feed does not call an unreadable record empty',
	!/nothing has happened yet/i.test(body),
);

// ── 2. Host page: an unreadable drift check must not render as in spec ──────
await ctx.unroute?.('**/tasks*').catch(() => {});
const page2 = await ctx.newPage();
await breakRead(page2, '**/artifacts/drift*');
// Address the host directly rather than hunting for a row to click: which
// element opens a host is a layout question, and a drive that depends on it
// fails for reasons that have nothing to do with the fix under test.
// And it has to be a host with ARTIFACT HISTORY. A host with no artifacts has
// no drift verdict to be unknown about, so the line is correctly absent there -
// the first run of this drive failed against exactly such a host and the fix
// was behaving properly. Pick one where the question means something.
const auth = { headers: { Authorization: `Bearer ${TOKEN}` } };
const listed = await ctx.request.get('/inventory?limit=30', auth);
let hostId = '';
if (listed.ok()) {
	for (const h of (await listed.json()).items ?? []) {
		const doc = await ctx.request.get(`/inventory/${h.id}/doc`, auth);
		if (!doc.ok()) continue;
		if (((await doc.json()).artifact_history ?? []).length > 0) {
			hostId = h.id;
			break;
		}
	}
}
if (hostId) {
	await page2.goto(`/ui/hosts/${hostId}`);
	await page2.waitForLoadState('networkidle');
	// The dry-run against 3.6.18 asserted against the LIST page because the
	// click had not navigated - a check that can pass or fail for a reason that
	// has nothing to do with the fix.
	const onHostPage = /\/ui\/hosts\/[^/]+/.test(page2.url());
	check('opened a host page', onHostPage, page2.url());
	const hostBody = await page2.locator('body').innerText();
	check(
		'host page says drift is unread',
		onHostPage && /cannot say whether this host/i.test(hostBody),
		hostBody.slice(0, 160).replace(/\s+/g, ' '),
	);
} else {
	check('host page drift-unread', false, 'no host to open');
}

// ── 3. The self-check must now report the reconcilers and the agent versions ─
const page3 = await ctx.newPage();
await page3.goto('/ui/settings?tab=subsystems');
await page3.waitForLoadState('networkidle');
const sub = await page3.locator('body').innerText();
// Assert on each subsystem's OWN consequence text, not on the word appearing
// anywhere: the dry-run passed the reconciler check against 3.6.18, which has
// no such subsystem, because the artifacts-remote consequence happens to say
// "the `archive_push` reconciler is scheduled".
check(
	'self-check reports the scheduled reconcilers as REGISTERED',
	/scheduled reconcilers are on time|reconciler\(s\) (are failing|have stopped|have never completed)/i.test(
		sub,
	),
);
// The first version of this check accepted the OFF text as a pass, so it went
// green while the MCP surface was reporting "No reconciler is registered" about
// an instance running seven of them. A live control plane HAS reconcilers;
// anything saying otherwise is the report being wrong, not the estate.
check(
	'self-check does NOT claim nothing maintains the estate',
	!/nothing maintains the estate on a timer/i.test(sub),
);
check(
	'self-check reports the fleet agent versions',
	/OLDER than this control plane|connected agents are running|no agent binary to be out of date|nothing can be said about what versions/i.test(
		sub,
	),
);
// The headline: dev's agent is genuinely behind, so this is an observation of
// the estate rather than a staged condition.
check(
	'self-check NAMES the stale agent',
	/OLDER than this control plane/i.test(sub),
	(sub.match(/.{0,80}OLDER than this control plane.{0,120}/i) || [''])[0],
);

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
