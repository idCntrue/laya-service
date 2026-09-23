# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
form on this repository (Security → Report a vulnerability). If that is
unavailable, open a minimal issue asking for a private channel and nothing else.

Please include:

- What the issue is and where (file, endpoint, configuration)
- How to reproduce it, or a proof of concept
- What an attacker gains
- Any suggested fix

**What to expect.** This is a small project without a dedicated security team.
Acknowledgment within a few days, and an assessment within a couple of weeks is
the realistic target. There is no bug bounty.

**Please do not** test against systems you do not own. If you want to
demonstrate a live issue, run the service locally — `make install-dev && make
run` is enough to reproduce almost anything.

## Supported versions

| Version | Supported |
|---|---|
| `main` branch | ✅ |
| Tagged releases | Most recent minor only |
| Anything older | ❌ |

This is pre-1.0 software. There are no backports.

## Threat model

Worth being explicit about, because the deployment guidance only makes sense in
light of it.

**In scope — things worth reporting:**

- Authentication bypass on a `/v1/*` route
- Leaking the API key through logs, error responses, or timing
- Remote code execution or path traversal via a request
- Crashes reachable from a malformed request body (DoS)
- Dependency confusion or a compromised dependency
- Bypassing the "refuse to start unauthenticated on a public interface" check

**Out of scope — expected behaviour, not vulnerabilities:**

- **No TLS.** The service speaks plain HTTP by design; terminate TLS at a
  reverse proxy. Sending a bearer token over plain HTTP on an untrusted network
  is a deployment mistake, not a bug here. See [README](README.md#security).
- **No rate limiting.** There is no admission control, and inference is
  CPU-expensive. Deploy behind a proxy if you need limits.
- **No request-body-size enforcement.** `MAX_BODY_BYTES` is validated as a
  configuration value but is not currently enforced by middleware. Known and
  documented; enforce it at the proxy.
- **Slow inference as a DoS vector.** Inherent to running a model on CPU.
- **Uncalibrated model output.** A correctness caveat about the model, not a
  security boundary. See [README](README.md#limitations-you-must-not-ignore).
- **The default `HOST=0.0.0.0`.** It is refused at startup unless an API key is
  set, which is the intended guard.

## Deployment security checklist

If you are putting this on a network, verify all of these:

- [ ] `LAYA_API_KEY` is set to a random value (`secrets.token_urlsafe(32)`),
      **not** a human-chosen string
- [ ] The key is in `.env`, which is gitignored, and has never been committed —
      check history if the repository was ever pushed: `git log -p -- .env`
- [ ] The security group / firewall restricts the port to known source ranges.
      Do **not** expose it to `0.0.0.0/0`
- [ ] TLS terminates at a reverse proxy in front of the service
- [ ] `CORS_ORIGINS` is empty unless a browser genuinely must call it directly,
      and lists exact origins — never `*`
- [ ] Rate limiting exists at the proxy if the endpoint is reachable by anyone
      you do not control
- [ ] `LOG_LEVEL` is not `DEBUG` in production (debug logs may include more
      request detail)
- [ ] You have rotated the key since any time it was transmitted unencrypted

## How the service protects itself

Controls that are already implemented, so you know what you are relying on:

| Control | Implementation |
|---|---|
| Constant-time key comparison | `hmac.compare_digest` in `middleware/auth.py` — a plain `==` leaks the key byte-by-byte through response timing |
| Uniform auth failures | 401 never distinguishes "no token" from "wrong token" |
| Auth before routing | Unknown paths return 401 rather than 404, so an unauthenticated caller cannot enumerate routes |
| Secret redaction in logs | `SecretRedactingFilter` scrubs bearer tokens and `key=value` credential patterns |
| No secrets in error bodies | 500/503 responses carry only a `request_id`; tracebacks go to the log |
| Request-ID sanitisation | Caller-supplied `X-Request-ID` must match `[A-Za-z0-9._:-]{1,128}`, blocking CRLF header injection and log forging |
| Refuse-to-start guard | Binding a non-loopback interface without an API key raises at startup |
| Non-root container | Docker image runs as uid 10001 |
| systemd hardening | `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `ProtectSystem=strict`, `ProtectHome=read-only`, and more — see `deploy/systemd/` |

## Known limitations

Documented honestly rather than left for you to discover:

1. **`MAX_BODY_BYTES` is not enforced.** It is a validated setting, not an
   active limit. A client can send a larger body than configured. Enforce it at
   the reverse proxy if this matters to you.
2. **No rate limiting or admission control.** Concurrent requests all contend
   for CPU, and each costs real compute.
3. **Single-process.** The model holds ~2 GB RSS; the deployment guidance warns
   against multiple workers on a small host.
4. **No key rotation overlap.** Rotating `LAYA_API_KEY` is briefly disruptive —
   there is no dual-key acceptance window.
5. **The model is downloaded at runtime** from a Hugging Face endpoint. If you
   point `HF_ENDPOINT` at a mirror you do not control, you are trusting that
   mirror to serve the weights it claims to. Pin `LAYA_MODEL` and verify the
   cache if that matters.
