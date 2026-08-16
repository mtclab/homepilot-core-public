import type { TaskStatus } from './api';

// Task status → badge class, reusing the shared artifact badge palette so a
// task's state reads the same everywhere (Overview / Agents / Tasks).
const TASK_STATUS_CLASSES: Record<TaskStatus, string> = {
	pending: 'badge-proposed',
	running: 'badge-approved',
	succeeded: 'badge-applied',
	failed: 'badge-failed',
	cancelled: 'badge-revoked',
};

export function taskStatusClass(status: string): string {
	return TASK_STATUS_CLASSES[status as TaskStatus] ?? 'badge-proposed';
}

// A task is cancellable only while still in flight. Terminal states
// (succeeded/failed/cancelled) are not.
export function isCancellable(status: string): boolean {
	return status === 'pending' || status === 'running';
}

// First segment of a task uuid — enough to identify a row without the noise of
// a full uuid. Falls back to the whole string for non-uuid ids.
export function shortTaskId(id: string): string {
	if (!id) return '';
	return id.split('-')[0];
}
