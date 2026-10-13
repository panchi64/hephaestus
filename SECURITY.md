# Security Policy

Hephaestus is a self-hosted AI workspace with privileged local capabilities — shell access, code execution, file writes, email, web research. Please do not run it as a public, unauthenticated service.

> **Note:** the backend is mid-rebuild (greenfield on Pydantic AI + FastAPI). This describes the **security posture the build targets**, defined in `docs/spec/00-overview.md` (`XC-SEC-*`, `XC-PORT-*`) and `docs/architecture/`. Concrete deployment hardening steps will be filled in as the install flow lands.

## Supported Versions

Security fixes are handled on the default branch until formal releases are cut.

## Security model

- **Single operator.** Hephaestus targets one operator; all data and features belong to them. When authentication is enabled, every request is authenticated before any feature is reached. (Multi-user privilege separation is out of scope, but every record carries an owner seam so it can be added later without a rewrite.)
- **Sensitive actions require explicit approval.** The agent must pause and ask for the operator's approval — showing the action and its concrete arguments — before anything powerful, externally visible, or hard to reverse takes effect: shell and code execution, filesystem writes, sending email, model download/serve/stop, endpoint/integration/webhook/token/settings configuration, and vault access. A denied action is not performed.
- **Encrypted at rest.** All user data is encrypted with confidentiality that remains secure against quantum-capable adversaries (AES-256 class). The encryption key is **derived from the operator's password and held only in memory** — there is no OS keystore dependency and nothing readable on disk. Authentication secrets (login password, TOTP seed, backup codes) are one-way hashed, never recoverable.
- **Untrusted content is data, not instructions.** External content (web pages, fetched URLs, emails, uploaded files, retrieved documents, transcripts, the active editor document) is marked untrusted before it enters a model prompt.
- **Platform-agnostic.** Runs on Linux, macOS, and other POSIX hosts with no OS-specific facility for core function.

## Deployment Guidance

- Keep authentication enabled for any network-accessible deployment.
- Serve plain HTTP only on `localhost`/trusted LAN; put a TLS-terminating reverse proxy in front for anything reachable beyond your machine — including a shared Tailscale IP. Without it, logins and tokens travel in cleartext.
- Put the app behind a trusted reverse proxy or private network; don't expose it directly to the public internet.
- Protect `.env`, `data/` (databases, keys, uploads, generated media), and logs. They are gitignored by default.
- Use a strong operator password and enable 2FA. Because the at-rest encryption key is derived from that password, its strength directly protects your data on disk.
- Rotate any API keys, webhook secrets, or tokens that appear in logs, screenshots, demos, or shared chats.

## Publishing A Fork

Before pushing a public fork, confirm no private files are staged:

```bash
git status --short
git check-ignore -v .env data/ logs/
git grep -n -I -E "(sk-[A-Za-z0-9_-]{20,}|xox[baprs]-|AIza[0-9A-Za-z_-]{20,}|Bearer [A-Za-z0-9._~+/-]{20,})"
```

Only `.env.example`, docs, source, tests, and frontend assets should be committed. Never commit live `data/` contents, local databases, uploaded files, generated media, logs, backups, API keys, password hashes, or personal documents.

## Reporting

Please report vulnerabilities privately via GitHub security advisories if available, or by opening a minimal issue that does not disclose exploit details.
