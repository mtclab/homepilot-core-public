# ADR-001: Zero-Secrets Vault Architecture

**Status:** Accepted  
**Date:** 2026-05-17  
**Deciders:** Architect, Security, PO

## Context

HomePilot previously stored secrets (API tokens, database credentials, encryption keys) in `.env` files deployed alongside application code. This created multiple security risks:

1. `.env` files could be accidentally committed to git
2. Secrets rotated infrequently because manual rotation was tedious
3. No audit trail for secret access
4. Deployment required secrets to be manually injected per environment
5. Container images could leak secrets through environment variable inspection

The system needed a way to manage secrets that was:
- Zero-knowledge by default (no secrets in code or config files)
- Automatable (secrets can be rotated via CLI without downtime)
- Auditable (log all secret access)
- Tamper-evident (detect if vault contents are modified)

## Decision

We adopted a **zero-secrets vault architecture** using `age` encryption with auto-generated passphrases:

1. **Vault storage**: All secrets are stored as age-encrypted files in `secrets/` directory, never in `.env` or code
2. **Auto-passphrase**: The vault passphrase is auto-generated on `hp init` and stored in the system keychain, never in plaintext files
3. **Two-phase bootstrap**: 
   - Phase 1: `hp init` generates passphrase, creates vault, stores in keychain
   - Phase 2: Application reads passphrase from keychain, decrypts vault at runtime
4. **Zero-secrets deployment**: Docker images and git repos contain zero secrets. Secrets are injected at runtime from the vault
5. **CLI interface**: `hp vault set/get/delete/list` commands for secret management
6. **Secret types**: Support for strings, JSON blobs, and base64-encoded binary data

### Architecture

```
┌─────────────┐    hp init    ┌──────────────┐
│  Keychain   │◄──────────────│  Passphrase   │
│  (storage)  │               │  (auto-gen)   │
└──────┬──────┘               └──────┬────────┘
       │                             │
       │  hp vault get               │  age decrypt
       ▼                             ▼
┌──────────────┐    age encrypt    ┌──────────────┐
│  Runtime     │◄──────────────────│  Vault Files  │
│  (env vars)  │                   │  (age-enc)    │
└──────────────┘                   └──────────────┘
```

## Consequences

### Positive
- Secrets never appear in git history, Docker images, or environment dump
- Rotation is a single CLI command (`hp vault set`), no container restart needed
- Each secret is individually encrypted; compromise of one doesn't compromise others
- Audit log of vault access via keychain entries

### Negative
- Requires `age` binary and system keychain access on deployment hosts
- Initial bootstrap requires interactive step (`hp init`) or pre-seeded keychain
- Backup strategy must include both vault files AND keychain passphrase
- Adds complexity to CI/CD — pipelines need vault access configured

### Risks
- **Keychain loss**: If the system keychain is lost and no passphrase backup exists, all vault secrets are unrecoverable. Mitigated by backup-strategy skill that archives passphrase separately.
- **age version compatibility**: Vault files are tied to age encryption format. Breaking age changes could affect decryption. Mitigated by pinning age version in dependencies.