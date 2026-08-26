import { render, screen } from '@testing-library/svelte';
import { describe, it, expect } from 'vitest';
import AttentionItem from './AttentionItem.svelte';

/**
 * The attention line (#549 F1, principle 1).
 *
 * The failure this forbids: an attention zone that tells the operator something
 * is wrong and then makes them go FIND it. Every item that has a fix surface
 * must be a link to that surface - "3 artifacts drifted" with no door is the
 * clutter the facelift exists to remove.
 */
describe('AttentionItem', () => {
	it('shows the severity chip, the line, and a door to the fix', () => {
		render(AttentionItem, {
			severity: 'critical',
			label: 'drifted',
			text: 'nginx.conf on web-01 disagrees with its plan',
			href: '/ui/changes/drift',
		});

		const link = screen.getByRole('link');
		expect(link).toHaveAttribute('href', '/ui/changes/drift');
		expect(link).toHaveTextContent('drifted');
		expect(link).toHaveTextContent('nginx.conf on web-01 disagrees with its plan');
	});

	it('paints severity as urgency, not as a lifecycle state', () => {
		const cases: Array<['critical' | 'warning' | 'notice', string]> = [
			['critical', 'badge-critical'],
			['warning', 'badge-warning'],
			['notice', 'badge-notice'],
		];
		for (const [severity, cls] of cases) {
			const { unmount } = render(AttentionItem, {
				severity,
				label: severity,
				text: 'something happened',
			});
			expect(screen.getByText(severity).className).toContain(cls);
			unmount();
		}
	});

	it('renders as plain text rather than a dead link when there is no fix surface', () => {
		render(AttentionItem, { label: 'offline', text: 'db-02 has not reported in 40m' });
		expect(screen.queryByRole('link')).toBeNull();
		expect(screen.getByText('db-02 has not reported in 40m')).toBeInTheDocument();
	});

	it('carries optional trailing context without a second line', () => {
		render(AttentionItem, {
			label: 'failed',
			text: 'apply nginx.conf',
			href: '/ui/records/tasks',
			meta: '12m ago',
		});
		expect(screen.getByText('12m ago')).toBeInTheDocument();
	});
});
