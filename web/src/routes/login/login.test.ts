import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Login from './+page.svelte';
import '../../lib/test-mocks';

describe('Login page', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('renders the login form with token input and connect button', () => {
		render(Login);

		expect(screen.getByLabelText(/api token/i)).toBeInTheDocument();
		expect(screen.getByRole('button', { name: /connect/i })).toBeInTheDocument();
		expect(screen.getByText(/homepilot/i)).toBeInTheDocument();
	});

	it('disables connect button when token is empty', () => {
		render(Login);

		const button = screen.getByRole('button', { name: /connect/i });
		expect(button).toBeDisabled();
	});

	it('enables connect button when token is entered', async () => {
		const user = userEvent.setup();
		render(Login);

		const input = screen.getByLabelText(/api token/i);
		await user.type(input, 'hp_test-token');

		const button = screen.getByRole('button', { name: /connect/i });
		expect(button).not.toBeDisabled();
	});
});