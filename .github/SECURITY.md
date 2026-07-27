# Security policy

This repository is public and backs a live service at **https://ashaat.dev**.
If you find a vulnerability, please report it privately first — an issue or a
pull request describing one is public the moment you open it.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: go to the repository's
**Security** tab → **Report a vulnerability**. That opens a private advisory
visible only to the maintainer, and it needs no email address from either side.

Please include what you need to make the finding reproducible:

- the request (method, path, headers that matter — redact any real credential),
- what you expected and what actually happened,
- the impact you believe it has.

Expect an acknowledgement within a few days. This is a personal project, not a
staffed security team, so please size your expectations accordingly — but a
real finding will be taken seriously and fixed.

## Scope

**In scope** — the deployed API and site: authentication and session handling,
API-key issuance and verification, quota and metering, the DCF endpoints, the
account UI, and anything that leaks a credential or another customer's data.

**Out of scope**

- Findings that require a compromised machine or a stolen credential to begin
  with.
- Denial of service by volume. The service runs on a platform whose job that
  is; please don't test it. *Amplification* — one request causing
  disproportionate work or upstream cost — **is** in scope and is interesting.
- Reports from automated scanners with no demonstrated impact.
- Missing headers or weak TLS settings with no exploitable consequence.
- Anything about the upstream data providers themselves (Financial Modeling
  Prep, Finnhub) or the accuracy of a valuation. The DCF output is a model
  estimate from caller-supplied assumptions, not investment advice; a number
  you disagree with is not a vulnerability.

## Testing guidance

Please test against your own account and your own API keys. Do not attempt to
access another customer's data, and stop as soon as you have enough to write
the report — proving a door is open does not require walking through it.

## What this project already does

Recorded so you don't spend time re-deriving it, and so the claims are
falsifiable. See `project-docs/issues.MD` for the full audit trail.

- API keys are stored as peppered HMAC-SHA256 hashes, never in plaintext; the
  full key is shown exactly once, at creation.
- Browser sessions and machine API keys are separate credential classes: a
  session cookie never authorizes a valuation, and an API key never reaches an
  account route.
- Session cookies are `HttpOnly`, `Secure`, and `SameSite=Lax`; state-changing
  account requests additionally require a double-submit CSRF token.
- Database access is service-role only and server-side only. Row-level security
  is on for every table, and every `SECURITY DEFINER` function has `EXECUTE`
  revoked from `PUBLIC`, so the anon key cannot reach them through PostgREST.
- Logs are scrubbed centrally — API keys, bearer tokens, credential-bearing
  URLs, and email addresses — including on the `httpx` logger this project does
  not own. Request paths are logged as route templates, never raw.
- Secrets live only in the deployment environment. No credential has ever been
  committed to this repository.
