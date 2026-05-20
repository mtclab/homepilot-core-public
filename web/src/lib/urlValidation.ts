const LOOPBACK = /^(localhost|127(?:\.\d+){3}|::1)$/;

export interface UrlValidation {
	error?: string;
	warning?: string;
}

export function validateApiBase(value: string): UrlValidation {
	const url = value.trim();
	if (!url) return {};

	let parsed: URL;
	try {
		parsed = new URL(url);
	} catch {
		return { error: 'Not a valid URL' };
	}

	if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
		return { error: 'URL must use http:// or https://' };
	}

	if (parsed.protocol === 'http:' && !LOOPBACK.test(parsed.hostname)) {
		return { warning: 'http:// is insecure for non-loopback addresses; prefer https://' };
	}

	return {};
}
