import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '../test-mocks';
import NotificationChannels from './NotificationChannels.svelte';
import { api, type NotificationChannel } from '$lib/api';
import { notify } from '$lib/stores';

/**
 * Notification channels (#660): one ntfy topic URL and alerts reach a phone.
 *
 * Asserted as the operator meets it: the URL goes in once and is never shown
 * again (the list says the host only), "Send test" reports a refusal as a
 * refusal with its reason, and the last-delivery column says whether the
 * channel has ever proved it works.
 *
 * Teeth: render a stored URL instead of `endpoint` and the redaction case
 * fails; report every test as delivered and the refused case fails; show
 * "ok" for a channel with no delivery and the untested case fails.
 */
const list = api.listNotificationChannels as ReturnType<typeof vi.fn>;
const create = api.createNotificationChannel as ReturnType<typeof vi.fn>;
const test = api.testNotificationChannel as ReturnType<typeof vi.fn>;
const notifyMock = notify as unknown as ReturnType<typeof vi.fn>;

const CHANNEL: NotificationChannel = {
	id: 4,
	kind: 'ntfy',
	name: 'phone',
	endpoint: 'https://ntfy.example.com',
	has_token: true,
	enabled: true,
	created_at: '2026-11-07T06:00:00Z',
	last_attempt_at: null,
	last_ok_at: null,
	last_error: null,
	last_error_at: null,
	state: 'untested',
};

describe('NotificationChannels', () => {
	beforeEach(() => vi.clearAllMocks());

	it('lists a channel by host only and says it has never delivered', async () => {
		list.mockResolvedValue({ items: [CHANNEL], total: 1 });
		render(NotificationChannels);
		expect(await screen.findByText('https://ntfy.example.com · token')).toBeInTheDocument();
		expect(screen.getByText('never delivered - send a test')).toHaveAttribute('data-channel-state', 'untested');
		expect(document.body.textContent).not.toContain('/topic');
	});

	it('adds an ntfy channel from its topic URL and token', async () => {
		list.mockResolvedValue({ items: [], total: 0 });
		create.mockResolvedValue(CHANNEL);
		render(NotificationChannels);
		await screen.findByText(/No channel yet/);
		await fireEvent.input(screen.getByPlaceholderText('phone'), { target: { value: 'phone' } });
		await fireEvent.input(screen.getByPlaceholderText('https://ntfy.example.com/topic'), {
			target: { value: 'https://ntfy.example.com/hp-alerts' },
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Add channel' }));
		await waitFor(() =>
			expect(create).toHaveBeenCalledWith({
				kind: 'ntfy',
				name: 'phone',
				url: 'https://ntfy.example.com/hp-alerts',
			}),
		);
	});

	it('reports a refused test as a refusal, with the reason', async () => {
		list.mockResolvedValue({ items: [CHANNEL], total: 1 });
		test.mockResolvedValue({
			id: 4,
			endpoint: 'https://ntfy.example.com',
			delivered: false,
			error: 'the server answered HTTP 403',
			channel: { ...CHANNEL, state: 'failing' },
		});
		render(NotificationChannels);
		await fireEvent.click(await screen.findByRole('button', { name: 'Send test' }));
		await waitFor(() =>
			expect(notifyMock).toHaveBeenCalledWith(expect.stringContaining('NOT accepted'), 'err'),
		);
		expect(notifyMock.mock.calls[0][0]).toContain('HTTP 403');
	});

	it('shows the last failure on a failing channel', async () => {
		list.mockResolvedValue({
			items: [{ ...CHANNEL, state: 'failing', last_error: 'the server answered HTTP 500', last_error_at: '2026-11-07T06:00:00Z' }],
			total: 1,
		});
		render(NotificationChannels);
		const cell = await screen.findByText(/HTTP 500/);
		expect(cell).toHaveAttribute('data-channel-state', 'failing');
	});
});
