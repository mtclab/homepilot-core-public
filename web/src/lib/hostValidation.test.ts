import { describe, it, expect } from 'vitest';
import { isValidHubHost } from './hostValidation';

describe('isValidHubHost', () => {
	it('accepts plain IPv4 addresses', () => {
		expect(isValidHubHost('10.0.0.1')).toBe(true);
		expect(isValidHubHost('127.0.0.1')).toBe(true);
		expect(isValidHubHost('255.255.255.255')).toBe(true);
	});

	it('accepts DNS hostnames', () => {
		expect(isValidHubHost('hub.example.com')).toBe(true);
		expect(isValidHubHost('homepilot')).toBe(true);
		expect(isValidHubHost('pve-node-1.internal')).toBe(true);
	});

	it('rejects empty / nullish input', () => {
		expect(isValidHubHost('')).toBe(false);
		expect(isValidHubHost(null)).toBe(false);
		expect(isValidHubHost(undefined)).toBe(false);
	});

	it('rejects values carrying shell metacharacters (injection)', () => {
		expect(isValidHubHost('host; rm -rf /')).toBe(false);
		expect(isValidHubHost('host && curl evil')).toBe(false);
		expect(isValidHubHost('$(whoami)')).toBe(false);
		expect(isValidHubHost('host`id`')).toBe(false);
		expect(isValidHubHost('host|nc')).toBe(false);
	});

	it('rejects values with whitespace', () => {
		expect(isValidHubHost('bad host')).toBe(false);
		expect(isValidHubHost(' host')).toBe(false);
	});

	it('rejects schemes, ports, and paths', () => {
		expect(isValidHubHost('http://host')).toBe(false);
		expect(isValidHubHost('host:8080')).toBe(false);
		expect(isValidHubHost('host/path')).toBe(false);
	});
});
