import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
// test-mocks MUST come before the components, or they resolve the real $lib/api.
import '../test-mocks';
import EnrollAgent from './EnrollAgent.svelte';
import JoinRequests from './JoinRequests.svelte';
import { api } from '$lib/api';

/**
 * Agents can join (lane A3).
 *
 * 1. The enrol panel shows the one-liner the SERVER built - installer and agent
 *    binary from this control plane (`--hp-api`), so a hand-installed agent is
 *    this hub's version - instead of a client-side string that fetched GitHub's
 *    `releases/latest`.
 * 2. A host refused at the hub is listed as "wants to join" with the one action
 *    that lets it in, and unused bootstrap tokens are listed with expiry.
 *
 * Teeth: put the old `releases/latest` string back in EnrollAgent.svelte and the
 * first two tests fail; drop JoinRequests' "wants to join" list or its button and
 * the panel tests fail.
 */

const getBootstrapToken = api.getBootstrapToken as ReturnType<typeof vi.fn>;
const getEnrolmentRequests = api.getEnrolmentRequests as ReturnType<typeof vi.fn>;
const openEnrolmentWindow = api.openEnrolmentWindow as ReturnType<typeof vi.fn>;

const FROM_HERE =
	"( f=\"$(mktemp)\" && trap 'rm -f \"$f\"' EXIT && curl -fsSL -H 'x-hp-agent-token: hpbat_x' http://hub.example.com:8000/agents/dist/install-agent.sh -o \"$f\" && bash \"$f\" --hp-api http://hub.example.com:8000 --hub hub.example.com:8443 --token hpbat_x ) || { echo 'HomePilot agent install FAILED - see the error above' >&2; false; }";

const BOOTSTRAP = {
	bootstrap_token: 'hpbat_x',
	hub_host: 'hub.example.com',
	hub_port: 8443,
	hub_tls: false,
	hub_cert_sha256: '',
	install_command: FROM_HERE,
	install_source: 'control_plane',
	install_version: '3.7.0',
	install_fallback_reason: null,
};

const CLOSED = { open: false, expires_at: null, seconds_remaining: 0, fleet_empty: false };
const EMPTY_SUMMARY = { wants_to_join: 0, refused: 0, pending_tokens: 0, expired_unused_tokens: 0 };

const KNOCKING = {
	hostname: 'new-box',
	peer: '192.0.2.20',
	reason_code: 'window_closed',
	reason: 'shared-token enrolment refused: no enrolment window is open',
	agent_id: 'hp-agent-abc',
	first_seen: '2026-11-07T10:00:00Z',
	last_seen: '2026-11-07T10:30:00Z',
	attempts: 61,
	wants_to_join: true,
};

describe('EnrollAgent: the one-liner installs from this control plane', () => {
	beforeEach(() => vi.clearAllMocks());

	it('renders the command the server built, verbatim', async () => {
		getBootstrapToken.mockResolvedValue(BOOTSTRAP);
		render(EnrollAgent, { props: { show: 'bootstrap' } });

		await fireEvent.click(screen.getByRole('button', { name: 'Generate Bootstrap Token' }));

		const cmd = await screen.findByTestId('install-command');
		expect(cmd.textContent?.trim()).toBe(FROM_HERE);
		expect(screen.getByTestId('install-source').textContent).toMatch(/from this HomePilot/);
		// The hub is raw TCP; nothing on the panel calls it an http:// address.
		expect(document.body.textContent).not.toContain('http://hub.example.com:8443');
	});

	it('never builds a GitHub latest one-liner of its own', () => {
		const source = readFileSync(
			resolve(process.cwd(), 'src/lib/components/EnrollAgent.svelte'),
			'utf8'
		);
		expect(source).not.toMatch(/releases\/latest/);
		expect(source).not.toMatch(/github\.com/);
	});

	it('says so when it has to fall back to GitHub, and why', async () => {
		getBootstrapToken.mockResolvedValue({
			...BOOTSTRAP,
			install_command: 'curl -fsSL https://example.com/v3.7.0/install-agent.sh | bash -s --',
			install_source: 'github',
			install_version: 'v3.7.0',
			install_fallback_reason: 'install-agent.sh is not in this image',
		});
		render(EnrollAgent, { props: { show: 'bootstrap' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Generate Bootstrap Token' }));

		const note = await screen.findByTestId('install-source');
		expect(note.textContent).toMatch(/cannot serve the agent itself/);
		expect(note.textContent).toMatch(/not in this image/);
		expect(note.textContent).toMatch(/v3\.7\.0/);
	});

	it('shows no command when the server withheld one', async () => {
		getBootstrapToken.mockResolvedValue({ ...BOOTSTRAP, install_command: null });
		render(EnrollAgent, { props: { show: 'bootstrap' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Generate Bootstrap Token' }));

		await screen.findByText('Install one-liner unavailable');
		expect(screen.queryByTestId('install-command')).toBeNull();
	});
});

describe('JoinRequests: a refused agent is seen and can be let in', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		// Drop any queued once-values a previous test left unconsumed.
		getEnrolmentRequests.mockReset();
	});

	it('lists a knocking host as wanting to join, and opens the window for it', async () => {
		getEnrolmentRequests.mockResolvedValueOnce({
			attempts: [KNOCKING],
			pending_tokens: [],
			window: CLOSED,
			summary: { ...EMPTY_SUMMARY, wants_to_join: 1 },
		});
		getEnrolmentRequests.mockResolvedValueOnce({
			attempts: [KNOCKING],
			pending_tokens: [],
			window: { open: true, expires_at: '2026-11-07T10:45:00Z', seconds_remaining: 900, fleet_empty: false },
			summary: { ...EMPTY_SUMMARY, wants_to_join: 1 },
		});
		openEnrolmentWindow.mockResolvedValue({ ...CLOSED, open: true });

		render(JoinRequests, { props: { canWrite: true } });

		const row = await screen.findByTestId('wants-to-join');
		expect(row.textContent).toMatch(/new-box/);
		expect(row.textContent).toMatch(/192\.0\.2\.20/);
		expect(row.textContent).toMatch(/wants to join/);
		expect(row.textContent).toMatch(/refused 61 times/);

		await fireEvent.click(screen.getByRole('button', { name: /Open enrolment window/ }));
		expect(openEnrolmentWindow).toHaveBeenCalledWith(15);
		await waitFor(() =>
			expect(screen.getByTestId('join-window-state').textContent).toMatch(/open until/)
		);
		expect(screen.queryByRole('button', { name: /Open enrolment window/ })).toBeNull();
	});

	it('offers no window button to a wrong-token host, and no button at all to a reader', async () => {
		getEnrolmentRequests.mockResolvedValue({
			attempts: [
				{ ...KNOCKING, hostname: 'typo-box', reason_code: 'bad_token', wants_to_join: false, reason: 'auth token matched nothing' },
			],
			pending_tokens: [],
			window: CLOSED,
			summary: { ...EMPTY_SUMMARY, refused: 1 },
		});
		render(JoinRequests, { props: { canWrite: true } });

		const row = await screen.findByTestId('refused-host');
		expect(row.textContent).toMatch(/typo-box/);
		expect(row.textContent).toMatch(/matched nothing/);
		expect(screen.queryByRole('button', { name: /Open enrolment window/ })).toBeNull();
	});

	it('lists unused bootstrap tokens with who minted them and when they expire', async () => {
		getEnrolmentRequests.mockResolvedValue({
			attempts: [],
			pending_tokens: [
				{ id: 'a1b2c3d4e5f6', minted_by: 'operator', created_at: '2026-11-07T10:00:00Z', expires_at: '2026-11-08T10:00:00Z', expired: false, seconds_remaining: 7200 },
				{ id: 'ffffeeeedddd', minted_by: 'zero-touch install on vm-7', created_at: null, expires_at: '2026-11-06T10:00:00Z', expired: true, seconds_remaining: 0 },
			],
			window: CLOSED,
			summary: { ...EMPTY_SUMMARY, pending_tokens: 1, expired_unused_tokens: 1 },
		});
		render(JoinRequests, { props: { canWrite: false } });

		const rows = await screen.findAllByTestId('pending-token');
		expect(rows[0].textContent).toMatch(/minted by operator/);
		expect(rows[0].textContent).toMatch(/expires in 2 h/);
		expect(rows[1].textContent).toMatch(/expired unused/);
	});

	it('takes no room when nobody is waiting', async () => {
		getEnrolmentRequests.mockResolvedValueOnce({
			attempts: [],
			pending_tokens: [],
			window: CLOSED,
			summary: EMPTY_SUMMARY,
		});
		render(JoinRequests, { props: { canWrite: true } });
		await waitFor(() => expect(getEnrolmentRequests).toHaveBeenCalled());
		expect(screen.queryByTestId('join-requests')).toBeNull();
	});
});
