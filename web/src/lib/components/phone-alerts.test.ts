import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '../test-mocks';
import PhoneAlerts from './PhoneAlerts.svelte';
import * as apiModule from '$lib/api';
import type { PhoneAlertsStatus, BundledNtfyStatus } from '$lib/api';

/**
 * "Get alerts on your phone" (#822), as the operator meets it.
 *
 * - A fresh install is offered three choices and nothing is set up until one
 *   is chosen (owner decision: opt-in).
 * - With HomePilot's own server chosen but not started, the page gives the ONE
 *   command and keeps checking by itself.
 * - A new phone's password is on screen once, with the subscribe QR code; it
 *   is not in anything the page reads again.
 * - "Done" needs every part proven: server up with HomePilot's config, the
 *   phone's address answering, a phone that may read, a delivered test push.
 * - ntfy.sh is never chosen without the plain warning on screen first.
 * - The honest limit is on the page with its second-channel offer.
 *
 * Teeth: show "Done" before a delivered test and the done test fails; drop the
 * warning paragraph and the ntfy.sh test fails; keep the password in `status`
 * (reloaded) instead of the add answer and the "once" test fails.
 */
const m = apiModule as unknown as Record<string, ReturnType<typeof vi.fn>>;
const api = apiModule.api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const LINK = 'ntfy://ntfy.example.com/homepilot?display=HomePilot';

function bundled(over: Partial<BundledNtfyStatus> = {}): PhoneAlertsStatus {
	return {
		bundled: {
			enabled_at: '2026-11-18T00:00:00Z',
			channel_id: 7,
			topic: 'homepilot',
			server: { running: true, configured: true, version: '2.37.2', detail: null },
			public_url: 'https://ntfy.example.com',
			public_url_ok_at: '2026-11-18T00:01:00Z',
			subscribe_link: LINK,
			phones: [],
			limit: 'This ntfy server runs on the same host as HomePilot. If that host goes down, nothing is left to push.',
			...over,
		},
		bundled_unavailable: null,
		compose_command: 'docker compose --profile ntfy up -d',
		ntfy_version: '2.37.2',
	};
}

const FRESH: PhoneAlertsStatus = {
	bundled: null,
	bundled_unavailable: null,
	compose_command: 'docker compose --profile ntfy up -d',
	ntfy_version: '2.37.2',
};

describe('PhoneAlerts', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		cleanup();
	});

	it('offers three choices on a fresh install and sets up nothing by itself', async () => {
		m.getPhoneAlerts.mockResolvedValue(FRESH);
		render(PhoneAlerts);
		expect(await screen.findByText("HomePilot's own ntfy server")).toBeInTheDocument();
		expect(screen.getByText('An ntfy server you already run')).toBeInTheDocument();
		expect(screen.getByText('ntfy.sh')).toBeInTheDocument();
		expect(m.enableBundledNtfy).not.toHaveBeenCalled();
		expect(m.chooseNtfySh).not.toHaveBeenCalled();
	});

	// An install without HP_NTFY_INTERNAL_URL (no shipped compose) answered the
	// status with a 503 and the page showed only that error - ntfy.sh and an
	// existing server, which need no bundled service, were unreachable too.
	it('without an internal ntfy address, still offers the other two ways and says why the bundled one is off', async () => {
		const reason = 'HP_NTFY_INTERNAL_URL is not set, so HomePilot does not know where the bundled ntfy service answers.';
		m.getPhoneAlerts.mockResolvedValue({ ...FRESH, bundled_unavailable: reason });
		render(PhoneAlerts);
		const bundledRadio = (await screen.findByText("HomePilot's own ntfy server"))
			.closest('label')!
			.querySelector('input')!;
		expect(bundledRadio).toBeDisabled();
		expect(document.querySelector('[data-bundled-unavailable]')?.textContent).toContain(reason);
		expect(screen.queryByRole('button', { name: 'Set it up' })).not.toBeInTheDocument();
		// The way it starts on is usable: the existing-server form is there.
		expect(screen.getByRole('button', { name: 'Add and send a test' })).toBeInTheDocument();
		await fireEvent.click(screen.getByText('ntfy.sh'));
		expect(screen.getByRole('button', { name: 'Use ntfy.sh' })).toBeInTheDocument();
		expect(m.enableBundledNtfy).not.toHaveBeenCalled();
	});

	it('choosing the bundled server enables it, then gives the one command while it waits', async () => {
		m.getPhoneAlerts.mockResolvedValueOnce(FRESH);
		const waiting = bundled({
			server: { running: false, configured: false, version: null, detail: 'the ntfy service does not answer (ConnectError)' },
			public_url: null,
			public_url_ok_at: null,
			subscribe_link: null,
		});
		m.enableBundledNtfy.mockResolvedValue(waiting);
		m.getPhoneAlerts.mockResolvedValue(waiting);
		render(PhoneAlerts);
		await fireEvent.click(await screen.findByRole('button', { name: 'Set it up' }));
		await waitFor(() => expect(m.enableBundledNtfy).toHaveBeenCalledTimes(1));
		const cmd = await screen.findByText('docker compose --profile ntfy up -d', {
			selector: '[data-compose-command]',
		});
		expect(cmd).toBeInTheDocument();
		expect(screen.getByText(/checks again by itself/)).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Add a phone' })).toBeDisabled();
	});

	it("shows a new phone's password once, with the subscribe code", async () => {
		m.getPhoneAlerts.mockResolvedValue(bundled());
		m.addPhone.mockResolvedValue({
			name: 'pixel',
			username: 'pixel',
			password: 'abcd-efgh-ijkl-mnop',
			topic: 'homepilot',
			server_url: 'https://ntfy.example.com',
			subscribe_link: LINK,
		});
		const { unmount } = render(PhoneAlerts);
		await fireEvent.input(await screen.findByPlaceholderText('my-phone'), { target: { value: 'pixel' } });
		m.getPhoneAlerts.mockResolvedValue(bundled({ phones: [{ name: 'pixel', can_read: true }] }));
		await fireEvent.click(screen.getByRole('button', { name: 'Add a phone' }));
		expect(await screen.findByText('abcd-efgh-ijkl-mnop')).toBeInTheDocument();
		expect(m.addPhone).toHaveBeenCalledWith('pixel');
		const qr = document.querySelector('[data-new-phone] svg[data-qr]');
		expect(qr?.getAttribute('data-qr')).toBe(LINK);
		expect(screen.getByText(/shown this once/)).toBeInTheDocument();

		// Leave and come back: the password is gone - nothing the page reads has it.
		unmount();
		render(PhoneAlerts);
		expect(await screen.findByText('pixel')).toBeInTheDocument();
		expect(document.body.textContent).not.toContain('abcd-efgh-ijkl-mnop');
	});

	it('says done only once the test push was delivered', async () => {
		m.getPhoneAlerts.mockResolvedValue(bundled({ phones: [{ name: 'pixel', can_read: true }] }));
		api.testNotificationChannel.mockResolvedValue({
			id: 7,
			endpoint: 'http://ntfy',
			delivered: true,
			error: null,
			channel: {},
		});
		render(PhoneAlerts);
		await screen.findByRole('button', { name: 'Send a test push' });
		expect(document.querySelector('[data-phone-alerts-done]')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Send a test push' }));
		await waitFor(() => expect(api.testNotificationChannel).toHaveBeenCalledWith(7));
		expect(await screen.findByText('Done: alerts reach your phone.')).toBeInTheDocument();
	});

	it('is not done when the address does not answer, even with a delivered test', async () => {
		m.getPhoneAlerts.mockResolvedValue(
			bundled({ phones: [{ name: 'pixel', can_read: true }], public_url_ok_at: null }),
		);
		api.testNotificationChannel.mockResolvedValue({ id: 7, endpoint: 'http://ntfy', delivered: true, error: null, channel: {} });
		render(PhoneAlerts);
		await fireEvent.click(await screen.findByRole('button', { name: 'Send a test push' }));
		await waitFor(() => expect(api.testNotificationChannel).toHaveBeenCalled());
		expect(document.querySelector('[data-phone-alerts-done]')).toBeNull();
	});

	it('states the limit and offers a second channel', async () => {
		m.getPhoneAlerts.mockResolvedValue(bundled());
		render(PhoneAlerts);
		expect(await screen.findByText(/same host as HomePilot/)).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Add ntfy.sh as a second channel' })).toBeEnabled();
	});

	it('lets an adopted login that cannot read be allowed', async () => {
		m.getPhoneAlerts.mockResolvedValue(bundled({ phones: [{ name: 'old-phone', can_read: false }] }));
		m.allowPhone.mockResolvedValue({ name: 'old-phone', can_read: true });
		render(PhoneAlerts);
		expect(await screen.findByText('cannot read the alerts')).toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Allow' }));
		await waitFor(() => expect(m.allowPhone).toHaveBeenCalledWith('old-phone'));
	});

	it('shows the ntfy.sh warning BEFORE it is chosen, then the topic once with its code', async () => {
		m.getPhoneAlerts.mockResolvedValue(FRESH);
		m.chooseNtfySh.mockResolvedValue({
			channel: { id: 9 },
			topic_url: 'https://ntfy.sh/hp-abcdefghijklmnopqrstuvwx',
			subscribe_link: 'ntfy://ntfy.sh/hp-abcdefghijklmnopqrstuvwx?display=HomePilot',
			warning: 'ntfy.sh is a public server run by someone else.',
		});
		api.testNotificationChannel.mockResolvedValue({ id: 9, endpoint: 'https://ntfy.sh', delivered: true, error: null, channel: {} });
		render(PhoneAlerts);
		await fireEvent.click(await screen.findByLabelText(/ntfy\.sh/));
		const warning = await screen.findByText(/leaves your network/, { selector: '[data-ntfysh-warning]' });
		expect(warning).toBeInTheDocument();
		expect(m.chooseNtfySh).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Use ntfy.sh' }));
		expect(await screen.findByText('https://ntfy.sh/hp-abcdefghijklmnopqrstuvwx')).toBeInTheDocument();
		await waitFor(() => expect(api.testNotificationChannel).toHaveBeenCalledWith(9));
		expect(document.querySelector('[data-ntfysh] svg[data-qr]')).not.toBeNull();
	});

	it('an existing server: adds the channel, sends a test, and gives its subscribe code', async () => {
		m.getPhoneAlerts.mockResolvedValue(FRESH);
		api.createNotificationChannel.mockResolvedValue({ id: 3 });
		api.testNotificationChannel.mockResolvedValue({ id: 3, endpoint: 'http://ntfy.example.com', delivered: true, error: null, channel: {} });
		render(PhoneAlerts);
		await fireEvent.click(await screen.findByLabelText(/already run/));
		await fireEvent.input(screen.getByPlaceholderText('https://ntfy.example.com/hp-alerts'), {
			target: { value: 'http://ntfy.example.com/hp-alerts' },
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Add and send a test' }));
		await waitFor(() =>
			expect(api.createNotificationChannel).toHaveBeenCalledWith({
				kind: 'ntfy',
				name: 'Phone',
				url: 'http://ntfy.example.com/hp-alerts',
			}),
		);
		await waitFor(() => expect(api.testNotificationChannel).toHaveBeenCalledWith(3));
		const qr = await waitFor(() => {
			const el = document.querySelector('[data-existing-done] svg[data-qr]');
			expect(el).not.toBeNull();
			return el;
		});
		expect(qr?.getAttribute('data-qr')).toBe(
			'ntfy://ntfy.example.com/hp-alerts?display=HomePilot&secure=false',
		);
	});
});
