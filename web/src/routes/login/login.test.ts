import { render, screen, waitFor } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST be imported before the component: module evaluation follows
// import order, so a component imported first resolves the REAL $lib/api and
// never sees the mock (its api calls then hit fetch and silently fail).
import '../../lib/test-mocks';
import Login from './+page.svelte';
import { goto } from '../../lib/test-mocks';
import { api, setToken } from '$lib/api';

// The route asks the backend whether the instance has ever been claimed before
// it decides which screen to draw, so every assertion here waits for that
// answer rather than reading the first frame.
const claimStatus = api.claimStatus as ReturnType<typeof vi.fn>;
const claimInstance = api.claimInstance as ReturnType<typeof vi.fn>;

describe('Login page (claimed instance)', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		claimStatus.mockResolvedValue({ state: 'claimed' });
	});

	it('renders the login form with token input and connect button', async () => {
		render(Login);

		expect(await screen.findByLabelText(/api token/i)).toBeInTheDocument();
		expect(screen.getByRole('button', { name: /connect/i })).toBeInTheDocument();
		expect(screen.getByText(/homepilot/i)).toBeInTheDocument();
	});

	it('disables connect button when token is empty', async () => {
		render(Login);

		const button = await screen.findByRole('button', { name: /connect/i });
		expect(button).toBeDisabled();
	});

	it('enables connect button when token is entered', async () => {
		const user = userEvent.setup();
		render(Login);

		const input = await screen.findByLabelText(/api token/i);
		await user.type(input, 'hp_test-token');

		const button = screen.getByRole('button', { name: /connect/i });
		expect(button).not.toBeDisabled();
	});

	it('never offers the claim screen on a claimed instance', async () => {
		render(Login);

		await screen.findByLabelText(/api token/i);
		expect(screen.queryByLabelText(/claim code/i)).not.toBeInTheDocument();
		expect(
			screen.queryByRole('button', { name: /claim this instance/i }),
		).not.toBeInTheDocument();
	});
});

describe('Login page (unclaimed, reached from its own network)', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		claimStatus.mockResolvedValue({ state: 'unclaimed', code_required: false });
	});

	it('shows the claim screen with NO code field and says why to claim now', async () => {
		render(Login);

		expect(await screen.findByRole('button', { name: /claim this instance/i })).toBeEnabled();
		expect(screen.queryByLabelText(/claim code/i)).not.toBeInTheDocument();
		expect(screen.queryByLabelText(/^api token$/i)).not.toBeInTheDocument();
		expect(screen.getByText(/anyone else on this network could claim it/i)).toBeInTheDocument();
		expect(screen.getByLabelText(/proxmox address/i)).toBeInTheDocument();
		expect(screen.getByLabelText(/proxmox api token/i)).toBeInTheDocument();
	});

	it('claims with nothing typed and lands on the dashboard with the new token', async () => {
		const user = userEvent.setup();
		claimInstance.mockResolvedValue({
			token: 'hp_minted-by-the-claim',
			scope: 'full',
			proxmox_configured: false,
		});
		render(Login);

		await user.click(await screen.findByRole('button', { name: /claim this instance/i }));

		// THE GOAL: the token the claim returned is the session, and the operator
		// ends up where a normal login ends up - not on a "claimed!" dead end.
		await waitFor(() => expect(setToken).toHaveBeenCalledWith('hp_minted-by-the-claim'));
		expect(claimInstance).toHaveBeenCalledWith(expect.objectContaining({ code: undefined }));
		expect(api.login).toHaveBeenCalledWith('hp_minted-by-the-claim');
		expect(goto).toHaveBeenCalledWith('/ui/artifacts');
	});

	it('passes the Proxmox details through when they are filled in', async () => {
		const user = userEvent.setup();
		claimInstance.mockResolvedValue({ token: 'hp_x', scope: 'full', proxmox_configured: true });
		render(Login);

		await user.type(await screen.findByLabelText(/proxmox address/i), 'pve.example.com');
		await user.type(screen.getByLabelText(/proxmox api token/i), 'root@pam!hp=uuid');
		await user.click(screen.getByRole('button', { name: /claim this instance/i }));

		await waitFor(() =>
			expect(claimInstance).toHaveBeenCalledWith(
				expect.objectContaining({
					proxmox_host: 'pve.example.com',
					proxmox_token: 'root@pam!hp=uuid',
				}),
			),
		);
	});
});

describe('Login page (unclaimed, reached from outside its network)', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		claimStatus.mockResolvedValue({ state: 'unclaimed', code_required: true });
	});

	it('asks for the code and says where to get it', async () => {
		render(Login);

		expect(await screen.findByLabelText(/claim code/i)).toBeInTheDocument();
		expect(screen.getByText(/hp claim-code/i)).toBeInTheDocument();
		// Nothing can be claimed until the code is typed.
		expect(screen.getByRole('button', { name: /claim this instance/i })).toBeDisabled();
	});

	it('sends the code the operator typed', async () => {
		const user = userEvent.setup();
		claimInstance.mockResolvedValue({ token: 'hp_x', scope: 'full', proxmox_configured: false });
		render(Login);

		await user.type(await screen.findByLabelText(/claim code/i), 'hpc_thecode');
		await user.click(screen.getByRole('button', { name: /claim this instance/i }));

		await waitFor(() =>
			expect(claimInstance).toHaveBeenCalledWith(
				expect.objectContaining({ code: 'hpc_thecode' }),
			),
		);
	});

	it('keeps the operator on the claim screen when the code is refused', async () => {
		const user = userEvent.setup();
		claimInstance.mockRejectedValue(new Error('Invalid claim code'));
		render(Login);

		await user.type(await screen.findByLabelText(/claim code/i), 'hpc_wrong');
		await user.click(screen.getByRole('button', { name: /claim this instance/i }));

		expect(await screen.findByText(/invalid claim code/i)).toBeInTheDocument();
		expect(goto).not.toHaveBeenCalled();
		expect(screen.getByLabelText(/claim code/i)).toBeInTheDocument();
	});
});
