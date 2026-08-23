import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../lib/test-mocks';
import Layout from './+layout.svelte';

/**
 * The shell has to work on a phone, and with a keyboard (#445 B6, B5).
 *
 * The sidebar was a fixed 176px column that was always there, so on a phone it
 * ate the width the tables - which ARE the product - needed. Below `md` the nav
 * now collapses behind a bar.
 *
 * Teeth: render the nav unconditionally and "starts closed" fails; drop the
 * Escape handler and the keyboard test fails; drop `on:click` on the links and
 * the drawer stays open over the page a tap just loaded.
 */
describe('App shell: the small-screen nav', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('starts closed, so the page is the page', async () => {
		const { container } = render(Layout);

		const toggle = await screen.findByRole('button', { name: /open navigation/i });
		expect(toggle).toHaveAttribute('aria-expanded', 'false');
		// The nav element itself has to be out of the flow below `md`, not merely
		// reported as closed: the whole point is the width it was taking.
		const nav = container.querySelector('#main-nav');
		expect(nav?.className).toContain('hidden');
		expect(nav?.className).toContain('md:flex');
	});

	it('opens on the toggle and says so to a screen reader', async () => {
		render(Layout);
		const toggle = await screen.findByRole('button', { name: /open navigation/i });

		await fireEvent.click(toggle);

		await waitFor(() =>
			expect(screen.getByRole('button', { name: /close navigation/i })).toHaveAttribute(
				'aria-expanded',
				'true'
			)
		);
	});

	it('names what it controls', async () => {
		render(Layout);

		const toggle = await screen.findByRole('button', { name: /open navigation/i });
		expect(toggle).toHaveAttribute('aria-controls', 'main-nav');
	});

	it('closes on Escape - an overlay a keyboard cannot dismiss is a trap', async () => {
		render(Layout);
		const toggle = await screen.findByRole('button', { name: /open navigation/i });
		await fireEvent.click(toggle);
		await screen.findByRole('button', { name: /close navigation/i });

		await fireEvent.keyDown(window, { key: 'Escape' });

		await waitFor(() =>
			expect(screen.getByRole('button', { name: /open navigation/i })).toBeInTheDocument()
		);
	});

	it('closes when a nav link is followed', async () => {
		render(Layout);
		const toggle = await screen.findByRole('button', { name: /open navigation/i });
		await fireEvent.click(toggle);

		await fireEvent.click(screen.getByRole('link', { name: 'Changes' }));

		await waitFor(() =>
			expect(screen.getByRole('button', { name: /open navigation/i })).toBeInTheDocument()
		);
	});
});
