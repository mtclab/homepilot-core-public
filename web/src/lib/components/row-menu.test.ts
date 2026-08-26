import { render, screen } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import RowMenuFixture from './RowMenu.fixture.svelte';

/**
 * The row overflow menu (#549 F1, principle 4 / P7 vocabulary).
 *
 * The failure this forbids: a menu that is a mouse-only trap. Collapsing row
 * actions behind a trigger REMOVES affordances from the row, so the trigger has
 * to give them all back to a keyboard - open with Enter or Down, land inside,
 * leave with Escape and get focus back where it was. A menu that opens but
 * cannot be closed without a click is worse than the wall of buttons it
 * replaced.
 */
describe('RowMenu', () => {
	it('starts closed and names itself for the row it belongs to', () => {
		render(RowMenuFixture);
		const trigger = screen.getByRole('button', { name: 'Actions for web-01' });
		expect(trigger).toHaveAttribute('aria-expanded', 'false');
		expect(screen.queryByRole('menu')).toBeNull();
	});

	it('opens on click and reveals the row actions', async () => {
		const user = userEvent.setup();
		render(RowMenuFixture);

		await user.click(screen.getByRole('button', { name: 'Actions for web-01' }));

		expect(screen.getByRole('menu')).toBeInTheDocument();
		expect(screen.getByRole('menuitem', { name: 'Adopt' })).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Actions for web-01' })).toHaveAttribute(
			'aria-expanded',
			'true',
		);
	});

	it('opens from the keyboard and puts focus on the first action', async () => {
		const user = userEvent.setup();
		render(RowMenuFixture);

		screen.getByRole('button', { name: 'Actions for web-01' }).focus();
		await user.keyboard('{Enter}');

		expect(screen.getByRole('menu')).toBeInTheDocument();
		expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Adopt' }));
	});

	it('ArrowDown opens it too, the way a menu button is expected to', async () => {
		const user = userEvent.setup();
		render(RowMenuFixture);

		screen.getByRole('button', { name: 'Actions for web-01' }).focus();
		await user.keyboard('{ArrowDown}');

		expect(screen.getByRole('menu')).toBeInTheDocument();
	});

	it('Escape closes it and hands focus back to the trigger', async () => {
		const user = userEvent.setup();
		render(RowMenuFixture);
		const trigger = screen.getByRole('button', { name: 'Actions for web-01' });

		await user.click(trigger);
		await user.keyboard('{Escape}');

		expect(screen.queryByRole('menu')).toBeNull();
		expect(document.activeElement).toBe(trigger);
	});

	it('a click anywhere else dismisses it', async () => {
		const user = userEvent.setup();
		render(RowMenuFixture);

		await user.click(screen.getByRole('button', { name: 'Actions for web-01' }));
		expect(screen.getByRole('menu')).toBeInTheDocument();

		await user.click(screen.getByTestId('outside'));
		expect(screen.queryByRole('menu')).toBeNull();
	});

	it('clicking inside the menu does NOT dismiss it before the action runs', async () => {
		const user = userEvent.setup();
		render(RowMenuFixture);

		await user.click(screen.getByRole('button', { name: 'Actions for web-01' }));
		await user.click(screen.getByRole('menuitem', { name: 'Forget' }));

		// "Forget" does not close: an item decides for itself, via `let:close`.
		expect(screen.getByRole('menu')).toBeInTheDocument();
	});

	it('an item that asks to close closes the menu', async () => {
		const user = userEvent.setup();
		render(RowMenuFixture);

		await user.click(screen.getByRole('button', { name: 'Actions for web-01' }));
		await user.click(screen.getByRole('menuitem', { name: 'Adopt' }));

		expect(screen.queryByRole('menu')).toBeNull();
	});

	it('the trigger toggles rather than only opening', async () => {
		const user = userEvent.setup();
		render(RowMenuFixture);
		const trigger = screen.getByRole('button', { name: 'Actions for web-01' });

		await user.click(trigger);
		await user.click(trigger);

		expect(screen.queryByRole('menu')).toBeNull();
	});
});
