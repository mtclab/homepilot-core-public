import { describe, it, expect } from 'vitest';
import { validateApiBase } from './urlValidation';

describe('validateApiBase', () => {
	it('accepts empty string (same-origin mode)', () => {
		expect(validateApiBase('')).toEqual({});
		expect(validateApiBase('   ')).toEqual({});
	});

	it('accepts https:// for any host', () => {
		expect(validateApiBase('https://example.com')).toEqual({});
		expect(validateApiBase('https://api.internal:8443')).toEqual({});
	});

	it('accepts http:// for loopback', () => {
		expect(validateApiBase('http://localhost:8000')).toEqual({});
		expect(validateApiBase('http://127.0.0.1:8000')).toEqual({});
		expect(validateApiBase('http://localhost')).toEqual({});
	});

	it('warns on http:// for non-loopback', () => {
		const result = validateApiBase('http://192.168.1.100:8000');
		expect(result.warning).toMatch(/insecure/);
		expect(result.error).toBeUndefined();
	});

	it('errors on non-URL strings', () => {
		expect(validateApiBase('not-a-url').error).toBeTruthy();
		expect(validateApiBase('ftp://example.com').error).toBeTruthy();
		expect(validateApiBase('javascript:alert(1)').error).toBeTruthy();
	});
});
