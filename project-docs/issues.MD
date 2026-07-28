# Issues

## ✅ Option C implemented 2026-07-28 — status of the plan below

The owner gave the go-ahead ("start implementing the caching plan"), which is
the sign-off the plan asked for. **Option C shipped, defaulting to off.**

| Slice | Status |
| --- | --- |
| Phase 0 — measure `cache=` / `fmp_calls=` on a repeat | ⬜ **owner-side**, needs production + a real API key |
| Slice 0 — fix a genuinely broken statements cache | ⬜ not needed unless Phase 0 shows `cache=provider` |
| Slice 1 — `app/response_cache.py` with a configurable TTL | ✅ done |
| Slice 2 — route wiring | ✅ done |
| Slice 3 — edge/CDN caching | ✅ deliberately **not** built, as planned |
| Slice 4 — tests | ✅ done — 23 new, suite **492 → 515**, 94.77% |
| Slice 5 — ADR + cross-doc updates | ✅ done (**ADR-010**, not 009 — 009 was taken) |

Three things came out different from the plan, each recorded where it lives:

- **The ADR is ADR-010.** `ADR-009` already belonged to the raw-provider-evidence
  decision, so the numbering in the plan below is wrong wherever it says 009.
- **`computed_at` is no longer stripped.** The plan inherited "strip
  `request_id`/`computed_at`" from the 2026-07-14 module, where it existed only
  to reproduce an ETag. Re-stamping it now would hide exactly the staleness this
  work promised to state, so a hit returns the original — and the response tells
  you its own age without you having to know a cache exists.
- **The `generation` key segment was dropped**, not carried over. Nothing ever
  rotated it and reading it cost a Redis GET on every request; under a short TTL
  expiry is the invalidation mechanism.

Also fixed along the way: `project-docs/REQUEST_FLOW.md` had been stale since
ADR-008 (still documenting ETags, `304`s, and the generation pointer). Step 5 is
corrected and the rest is flagged, rather than silently rewritten.

**What is still owner-side:** the cache is off until `VALUATION_CACHE_TTL_SECONDS`
is set in Vercel, and the exit criteria at the bottom of this plan need a real
production request to close.

---

## Repeat identical requests are never served from cache — plan (2026-07-28)

**Reported:** "There is no cache in my API. When I make the same exact request
and I request it twice, it sends a request to the backend rather than retrieving
the answer from cache."

**Confirmed — and it is current design, not a regression.** Before planning a
fix, the finding has to be stated plainly: the valuation response cache was
**deliberately deleted**, and `no-store` was **deliberately added**, by an owner
decision on 2026-07-16 that is recorded as ADR-008. So "fixing" this is not
repairing something broken; it is **reversing a recorded product decision**, and
that deserves a deliberate sign-off rather than a patch. The plan below is
written so the reversal is explicit, bounded, and switchable back off.

### Evidence that no response cache exists

| Fact | Where |
| --- | --- |
| Valuation responses are `Cache-Control: no-store` | `app/api.py:1333` |
| The route's own comment says the response is never cached, only the statements | `app/api.py:1266`–`1278` |
| `compute_dcf` + `compute_sensitivity_grid` run on **every** request | `app/api.py:1313`–`1315` |
| A live Finnhub quote is fetched on **every** request | `app/api.py:1285`–`1299` |
| "The valuation request/response and the DCF math are **not** cached" | ADR-008, `project-docs/ARCHITECTURE_DECISIONS.md:198` |
| `app/response_cache.py` (145 lines) existed at `7be4500`, deleted in `8e30cf4` (2026-07-18) | `git show 7be4500:app/response_cache.py` |
| ETag / conditional-304 handling for this route was removed in the same change | this file, "Slices 2+3+4" |

The one cache that *does* exist and *is* working is the **financial-statements**
cache (L1 → Redis → Supabase snapshot → FMP), and it reports which layer answered
via the `cache=` log field — `l1` / `l2` / `database` / `provider`
(`app/fundamentals.py:564,574,602,673`).

### What a second identical request costs today

| Layer | Cost on the repeat request | Cached? |
| --- | --- | --- |
| Browser / CDN | full round trip | ❌ `no-store` forbids it |
| Auth + quota | 1 Supabase RPC (`check_and_increment`, `app/api.py:620`) | ❌ by design (must meter) |
| Statements | 1 Redis GET, or 1 Supabase read | ✅ **already works** — 0 FMP calls |
| Market price | 1 Finnhub HTTP call | ❌ ADR-008 forbids caching it |
| DCF + sensitivity grid | 10 pure-Python DCF runs | ❌ recomputed |

**Read this table before choosing a fix.** The expensive upstream dependency —
FMP — is *already* cached to zero on the repeat. What actually remains is one
Supabase RPC, one Redis GET, one Finnhub call, and a few milliseconds of
arithmetic. The Finnhub call is the dominant latency (measured at ~64 ms in
production on 2026-07-27) and the only real rate-limit exposure (free tier:
60 req/min).

### Phase 0 — establish which "cache miss" is meant (do first, no code)

The report is consistent with three different problems that have three different
fixes. One measurement separates them. Send the same request twice against
production with a real API key and read the structured log line for each:

```
GET /v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.08&projection_years=5
```

| What the 2nd request logs | What it means | Go to |
| --- | --- | --- |
| `cache=provider` or `fmp_calls=4` | **A real bug** — the statements cache is not holding. Fix this first; it is the only reading where money is being wasted. | Slice 0 |
| `cache=l2` / `cache=database`, `fmp_calls=0`, `finnhub_calls=1` | Working as designed. The request reaches the server because ADR-008 says it must. | Slice 1+ |
| No log line at all for the 2nd request | It never reached the server — something upstream already cached it. Nothing to fix. | — |

Also confirm Redis is actually configured in production (`UPSTASH_REDIS_REST_URL`
+ `_TOKEN`, or the legacy `KV_REST_API_*`, `app/redis_cache.py:42`–`47`). With
Redis absent, L1 is per-instance and Vercel churns instances, so the statements
cache degrades to a Supabase read per request — still no FMP call, but a round
trip that was supposed to be free. `GET /ready` reports Redis as `degraded` when
it is unreachable.

### Slice 0 — only if Phase 0 shows `cache=provider` on a repeat

Genuine defect; fix before anything else. Likely suspects, in order:

1. Redis unconfigured in production → every instance starts cold. Check `/ready`.
2. `_is_current()` (`app/fundamentals.py:357`–`366`) rejects any entry stored
   before the last 6 PM Eastern refresh boundary whenever the snapshot store is
   configured. An entry cached at 10:00 ET stops counting as current at 18:00 ET
   even though its TTL has hours left — by design (ADR-007), but it means the
   afternoon→evening transition sends traffic to the DB layer until the nightly
   refresh re-stamps `verified_at`.
3. `FUNDAMENTALS_TTL_SECONDS` (default `14400`) set too low in the environment.

### The decision this needs (owner)

Three options. They are not variations on one fix — they trade different things.

**Option A — change nothing; document it.** Phase 0 confirms the statements
cache works, and the answer to "why does it hit the backend twice" is "because
you required a live price on every call." Cost: zero. Delivers: nothing the
report asked for.

**Option B — cache the price-independent body, keep the price live.** Restore
the response cache but store it **price-free**: strip `current_price`,
`price_as_of`, `price_fetched_at`, `upside_pct` on write and re-inject them from
a fresh Finnhub call on read. ADR-008 stays intact — no price is ever cached.
Saves: the DCF recompute and the statements lookup. Keeps: `no-store`, the
Finnhub call, the quota RPC. **Honest assessment: this is close to worthless.**
It removes a few milliseconds of pure arithmetic and adds a Redis round trip to
do it, and it does not stop the request reaching the backend — which is the
actual complaint. Recorded here so it is not re-proposed later as if it were new.

**Option C — bounded-staleness response cache (what the report actually asks
for).** Cache the whole body, price included, for a short TTL, and let the
response say so. This is the only option where a repeat request can be answered
without a Finnhub call — and, with a client-visible `max-age`, without reaching
the server at all. It **reverses** ADR-008's "never cached" clause, so it needs
an ADR-009 recording the supersession and the reason.

### Recommendation — Option C, defaulting to off

Ship Option C behind `VALUATION_CACHE_TTL_SECONDS`, **default `0` (disabled =
today's exact behavior)**. The owner turns caching on by setting it to `30` or
`60` in Vercel, and turns it off again by setting it back to `0` — no redeploy of
new code, no revert. That way the code change is reviewable and tested on its own
merits, the product decision stays a config decision, and ADR-008's guarantee
remains literally true in any environment that leaves the default alone.

The staleness must be **stated, not hidden**: when a cached body is served, the
response has to let the caller see how old the price is rather than presenting it
as live.

### Implementation plan (Option C)

**Slice 1 — restore the cache module.**
- Recover the deleted file as the starting point: `git show
  7be4500:app/response_cache.py > app/response_cache.py`. It already implements
  everything structural — key = `dcf:v1:resp:{ticker}:{generation}:{fingerprint}`
  where the fingerprint covers the resolved assumptions, the `sensitivity` flag,
  and `model_version`; a 60 s TTL; per-request fields stripped on write and
  re-injected on read; errors never cached; every Redis failure fails open.
- Change from the 2026-07-14 version: `RESPONSE_CACHE_TTL_SECONDS` becomes
  configurable (`VALUATION_CACHE_TTL_SECONDS`, default `0`), and `0` means the
  module is never consulted at all — not a 0-second TTL, an actual short-circuit,
  so the disabled path costs no Redis call.
- Keep `request_id` and `computed_at` in the stripped set. Add nothing to it: the
  price fields are now deliberately **inside** the cached body, which is the whole
  point of Option C and the one line where it differs from Option B.

**Slice 2 — wire the route.**
- In `get_valuation` (`app/api.py:1215`), after the quota pre-flight and before
  `asyncio.gather`: on a cache hit, skip **both** `load_statements()` and
  `load_quote()`, re-inject `request_id`/`computed_at`, and return.
- **The quota RPC stays pre-flight and a cache hit is still metered.** A cached
  answer is still an answer the customer received; not charging for it makes the
  ledger disagree with reality and hands out free requests to whoever replays a
  URL. The old implementation got this right — keep it right.
- Set `Cache-Control` from the same setting: `private, max-age={ttl}` when the
  cache is on, `no-store` when it is off. `private`, never `public` — the body is
  served behind an API key and the response carries per-caller `X-RateLimit-*`
  headers.
- Surface the price age. `price_fetched_at` is already in the body, and on a hit
  it will correctly show the older timestamp — that is sufficient, provided the
  docs say so explicitly (Slice 5).

**Slice 3 — do NOT add edge/CDN caching in this slice.** Tempting, because the
API was designed for it (canonical query-param ordering, GET) — but auth and
quota run **inside** the function. An `s-maxage` that lets Vercel's edge answer
from cache serves the body to callers whose API key was never checked and whose
quota was never decremented. If edge caching is wanted later it needs the API key
in the cache key, and that is its own design note, not a header tweak.

**Slice 4 — tests.** `tests/test_response_cache.py` (357 lines) was deleted with
the module and is recoverable the same way. It must be extended, not just
restored:
- default `VALUATION_CACHE_TTL_SECONDS=0` → two identical requests produce
  **two** Finnhub calls and `no-store` (today's behavior gets pinned, so nobody
  enables caching by accident);
- TTL > 0 → two identical requests produce **one** Finnhub call, one FMP-free
  statements load, and identical bodies apart from `request_id`;
- a cache hit still decrements quota and still writes a `usage_events` row;
- differing assumptions, differing `sensitivity`, and a bumped `model_version`
  each miss the cache;
- 404/422/502/503 outcomes are never cached;
- Redis unreachable → falls open to a live compute, never a 5xx.

**Slice 5 — cross-doc updates.** These currently state the opposite of what the
code would do, and a stale doc here is how the 2026-07-13 `PUBLIC_BASE_URL`
incident happened:
- `project-docs/ARCHITECTURE_DECISIONS.md` — add **ADR-009** superseding ADR-008's
  "never cached" clause; state the bounded-staleness trade-off and that the
  default is off. Do not edit ADR-008's text; supersede it, the way ADR-007 was.
- `docs/index.html:747` — the "Live price & caching" section says the price is
  "never cached anywhere — not by the API, not by a CDN, not by your browser."
  That becomes false the moment the TTL is non-zero.
- `README.md:60`, `:90`–`:93` — the same claim; plus add the new env var to the
  configuration table near `:180`.
- `CLAUDE.md` — the response-cacheability rationale for choosing GET.
- Regenerate the OpenAPI snapshot only if the response schema actually changes.

### Risks to weigh before enabling

- **A cached price is a wrong price.** For a 30–60 s TTL on a slow-moving large
  cap this is immaterial; during a halt, a gap, or an earnings print it is not.
  This is the entire reason ADR-008 exists — the plan does not dismiss it, it
  turns it into a knob with a stated bound.
- **`max-age` on the client means repeat requests are not metered.** They never
  reach the function, so quota and `usage_events` under-count by exactly the
  cached repeats. If billing accuracy matters more than saving the round trip,
  ship the server-side cache but keep `max-age=0, must-revalidate` — the customer
  still pays a round trip, but it costs no Finnhub call and no compute.
- **Redis becomes load-bearing for latency**, though not for correctness — every
  path already fails open.

### Exit criteria

Two identical production requests, ~5 s apart, with the TTL set to 60: the first
logs `finnhub_calls=1`, the second logs a cache hit with `finnhub_calls=0`, both
return byte-identical bodies apart from `request_id`, both appear in
`usage_events`, and the quota counter drops by 2. With the TTL back at 0, the
same pair produces two Finnhub calls and `no-store` — proving the switch works in
both directions.

---

## Safety & security audit — 2026-07-26

Scope: the whole application (`app/`, `docs/`, `scripts/`, `supabase/migrations/`,
CI workflows, repo hygiene), read for **security**, **null safety**, and
**unhandled runtime errors**, with the fact that **this is a public repository**
treated as a standing assumption — anyone can read the code, the migrations, the
validation bounds, and the error taxonomy, so nothing here may rely on the source
being private.

Baseline at audit time: **461 tests passing, 94.88% coverage**, ruff + mypy clean.
Everything below is a defect the green suite did not catch.

**Findings are ranked by severity. Each one states how it was verified.** Items
marked *(reproduced)* were executed against the real code, not read off the page.

### What was checked and found clean

Recording this so the next audit does not redo it:

- **No secret has ever been committed.** `git log --all -S<value>` for every
  value in the local `.env`/`.env.local` (8 credentials) returns nothing but
  `PUBLIC_BASE_URL`, which is not a secret. `.gitignore` covers `.env*`, `data/`,
  `.vercel/`, and key/cert extensions. No secret-shaped literal in any tracked
  file (only deliberate fakes in `tests/test_observability.py`). `.mcp.json` is
  tracked and holds a URL, no token. `scripts/create_api_key.py` reads its
  credentials from the environment.
- **Path traversal on `/Pics/{filename}` is genuinely closed** *(reproduced:
  `/Pics/..` → 404, `/Pics/blob.png` → 200)*. Starlette decodes `%2F` before
  routing, so the segment cannot span directories, and the `resolve()` +
  `_PICS_DIR not in target.parents` check catches the rest.
- **No DOM XSS in the three shipped pages.** Every value that reaches the DOM
  goes through `textContent`; the only `innerHTML` uses assign the empty string
  to clear a container.
- **Database lockdown is correct.** RLS is enabled on all Phase 5/6 tables, and
  every `SECURITY DEFINER` function has `set search_path = public` plus an
  explicit `revoke execute … from public` / `grant … to service_role`, so the
  publishable anon key cannot reach the RPCs over PostgREST.
- **CI is not a supply-chain hole.** Both workflows are `permissions: contents:
  read`, neither uses `pull_request_target`, and neither consumes a secret.
- **The constant-time comparisons that matter are correct** — `hmac.compare_digest`
  on bytes for the cron secret and the metrics token, on the API-key hash digest,
  and one generic 401 for every internal-endpoint failure mode.

---

### 🔴 HIGH

- [x] **H1 — The sensitivity grid turns documented-valid requests into 422s.**
  `app/dcf_engine.py:246` (`compute_sensitivity_grid`). The grid shifts WACC by
  ±0.01 and terminal growth by ±0.005 and calls `compute_dcf` for each cell, but
  the only guard before that call is the Gordon condition (`wacc <= 0 or growth
  >= wacc`). `compute_dcf` re-runs the **full public bounds check**, so a shifted
  cell that falls outside `MIN_WACC…MAX_WACC` or `MIN_TERMINAL_GROWTH…
  MAX_TERMINAL_GROWTH` raises `DCFValidationError` — which propagates out of the
  grid and fails **the entire request**. `sensitivity` defaults to `true`, so
  this is the default path. *(Reproduced — every one of these is inside the
  documented input range and every one 422s:)*

  | Request | Result |
  | --- | --- |
  | `wacc=0.50` (documented max) | 422 `wacc: must be between 0.001 and 0.5` |
  | `terminal_growth=-0.10` (documented min) | 422 `terminal_growth: must be between -0.1 and 0.1` |
  | `terminal_growth=0.10` (documented max) | 422 `terminal_growth: must be between -0.1 and 0.1` |
  | `wacc=0.0105` | 422 `wacc: must be between 0.001 and 0.5` |

  Two things make this worse than a plain bug. The error **blames the caller's
  own input**, which is valid — the rejected value is one the server invented.
  And it contradicts the function's own docstring ("combinations that would break
  the Gordon formula … come back as None rather than erroring, so one bad corner
  doesn't cost the caller the whole grid") — the intent was already right, only
  the Gordon condition was implemented. Quota is consumed pre-flight, so every
  one of these failures also costs the customer a request.
  **Fix:** treat a cell that fails validation exactly like a cell that breaks
  Gordon — `None`, not an exception.

- [x] **H2 — Any non-ASCII CSRF token is an unauthenticated unhandled
  exception.** `app/accounts.py:163` (`csrf_tokens_match`). `hmac.compare_digest`
  raises `TypeError: comparing strings with non-ASCII characters is not
  supported` when given `str` arguments outside ASCII, and Starlette decodes both
  headers and cookies as **latin-1** — so any byte ≥ 0x80 in `X-CSRF-Token` **or**
  in the `pt_csrf` cookie produces a non-ASCII `str` and raises. *(Reproduced
  end-to-end through the ASGI app: `POST /v1/auth/logout` with
  `X-CSRF-Token: caf\xe9` → `TypeError` escaping the application; same for the
  cookie; the ASCII control case returns 200.)*

  Reachable **without any credential** on every CSRF-protected route:
  `/v1/auth/email/login`, `/v1/auth/logout`, and all four
  `/v1/account/keys/*` endpoints. Consequences: the request bypasses the entire
  error contract (no JSON `error` body, no `X-Request-ID`, no
  `Cache-Control: no-store`, no security headers), it is logged at **ERROR** with
  a traceback on every hit, and it counts as a 5xx in the metrics — so a trivial
  loop inflates the error budget, floods log storage, and trips any SLO alert
  built on `dcf_http_requests_total{status="500"}`.
  **Fix:** compare the two tokens as bytes, and reject anything that isn't a
  plausible token before comparing at all.

---

### 🟠 MEDIUM

- [x] **M1 — `/docs` and `/redoc` execute third-party JavaScript on the same
  origin as the signed-in account UI, with no CSP.** *(Verified live:
  `https://www.ashaat.dev/docs` → 200 with **no** `Content-Security-Policy`
  header; the page loads `cdn.jsdelivr.net` scripts and styles.)* The three
  first-party pages carry `_LANDING_PAGE_CSP`; FastAPI's generated docs pages do
  not, because they never pass through those routes. That origin is the same one
  that serves `/dcf`, holds the cookie-authenticated key-management API, and sets
  the **non-`HttpOnly`** `pt_csrf` cookie (it has to be readable by JS for the
  double-submit check). So a compromised CDN asset — or anything that can inject
  into those pages — gets same-origin script execution with everything it needs
  to mint an API key for any visitor who is signed in: read `pt_csrf`, `POST
  /v1/account/keys` with the browser's `pt_session` cookie attached
  automatically, exfiltrate the returned key.
  **Fix:** serve the docs pages through routes that attach a tight CSP —
  crucially `connect-src 'self'`, which blocks exfiltration to a third-party
  host even if a script does run. Residual risk (a hostile script acting
  *within* the origin) is stated below; turning the docs off entirely is an
  owner decision, recorded in `TODO.md`.

- [x] **M2 — An unauthenticated caller could make this service an amplifier
  against Supabase Auth.** `_account_context` → `get_current_customer`. Send any
  junk `pt_session` + `pt_refresh` to `/v1/auth/me` (or any account route) and
  the app dutifully asked Supabase about them: `/auth/v1/user`, then
  `/auth/v1/token`. *(Measured, not estimated: exactly `["/auth/v1/user",
  "/auth/v1/token"]` per request — 2 outbound auth calls bought with 1 inbound
  request and no credential. A valid refresh token makes it 3.)* The per-IP
  login limiter covers only `/v1/auth/github/login` and `/v1/auth/email/login`;
  nothing covered this path.

  **Fix shipped — local screening, not a rate limit.** A per-address budget was
  written first and **rejected after a test caught it signing out a real user**:
  the budget has to be checked *before* the session is verified, so a prober
  sharing an address (a NAT, an office, a mobile carrier) could log out
  everyone behind it — a worse defect than the one being fixed. What shipped
  instead has no false-positive mode at all: Supabase access tokens are JWTs, so
  a `pt_session` that is not a decodable JWT with an unexpired `exp` **cannot**
  be accepted upstream, and asking is pure waste. `is_plausible_access_token`
  (`app/accounts.py`) rejects those locally; the `pt_refresh` cookie gets the
  same cheap shape check. Rejecting can only ever convert an upstream 401 into a
  local one, so no reachable session is affected. The claims are read as
  untrusted attacker-supplied JSON and used **only** to decide whether the call
  is worth making — every token that passes is still verified in full by
  Supabase.
  **Effect:** junk credentials now cost **zero** upstream calls; the worst case
  for a well-formed forgery is one outbound call per inbound request, so the
  amplification is gone. Capping absolute volume is a platform/WAF concern, not
  the application's, and is not claimed here.
  *(This also fixed the test double: `FakeSupabaseBackend` was handing out
  opaque ids like `access-1` where real Supabase issues JWTs, so no test had
  ever exercised the real cookie shape.)*

- [x] **M3 — Dependencies have no upper bound, no lockfile, and no scanning.**
  `pyproject.toml`: `httpx>=0.27`, `fastapi>=0.115`, and eight `dev` deps with
  open ranges. There is no `requirements.txt`, no lock, and no
  `.github/dependabot.yml`. Every Vercel build re-resolves to whatever is newest
  at build time, so a broken or malicious release reaches production **with no
  commit of ours** — and, this being a public repo, the exact dependency surface
  is published for anyone to watch. A green CI run proves nothing about the set
  of packages the next deploy will install.
  **Fix:** bound the majors, and add Dependabot so upgrades arrive as reviewable
  PRs instead of as silent build-time drift.

- [x] **M4 — Vercel labels the cookie-setting page `Cache-Control: public`.**
  *(Verified live: `GET https://www.ashaat.dev/dcf` returns
  `Cache-Control: public, max-age=0, must-revalidate` **and**
  `Set-Cookie: pt_csrf=…`.)* The app sets no `Cache-Control` on its HTML routes,
  so Vercel supplies that default — and `public` on a `Set-Cookie` response is
  exactly what a shared cache is not supposed to store. `max-age=0,
  must-revalidate` keeps the practical risk low (a cache must revalidate before
  reuse), but the app should not be relying on a platform default to decide the
  cacheability of a response that carries a per-visitor token.
  **Fix:** the app states its own `Cache-Control` on every HTML and schema route
  rather than inheriting one.

- [ ] **M5 — CSRF is unsigned double-submit on a non-`__Host-` cookie.**
  `app/accounts.py:146`. `pt_csrf` is set with `Secure; SameSite=lax; Path=/` but
  **no `__Host-` prefix**, and the token is not bound to the session. Cookies
  can be written across a registrable domain: anything able to set a cookie for
  `ashaat.dev` — a hostile or compromised sibling subdomain, now or in the
  future — can inject a `pt_csrf` value of its choosing and then send the
  matching header, satisfying the check. `SameSite=lax` is the real protection
  today (these are all `POST`s), which means the CSRF token is currently the
  *weaker* of the two defenses, not a second independent one.
  **Left open deliberately:** the fix is a cookie **rename** (`__Host-pt_csrf`,
  which browsers refuse to accept from a subdomain) or an HMAC binding the token
  to the session, and either one invalidates the token every live browser
  currently holds. It is a coordinated change to `docs/index.html` plus the
  server, not a mid-audit edit. Tracked in `TODO.md` §8.

---

### 🟡 LOW

- [x] **L1 — Fail-closed storage errors can surface as unhandled 500s instead.**
  `app/supabase.py:161` reads `payload["allowed"]`, `payload["limit"]`,
  `payload["remaining"]`, `payload["reset_epoch"]`, `payload["retry_after"]` off
  the quota RPC result. A shape change raises `KeyError`, which the middleware's
  `except SupabaseError` does **not** catch — so the request 500s instead of
  returning the intended 503 `auth_storage_unavailable`. Same class of gap:
  `_parse_datetime` raising `ValueError` on a malformed `expires_at`
  (`app/supabase.py:709`), and `int(record.get("daily_quota") or 100)` on a
  non-numeric column. The quota RPC is the one that decides whether a caller may
  be served, so its failure mode should never be "unhandled".

- [x] **L2 — `fetch_fundamentals` does not type-check the profile element.**
  `app/providers/fmp.py:270`. For the non-`limit` endpoint (`profile`) a list
  payload is reduced with `results[endpoint] = payload[0]` and no `isinstance`
  check, so a non-dict element flows into `normalize_fmp_fundamentals`, which
  calls `f.profile.get("sector")` → `AttributeError` → unhandled 500. The sibling
  method `fetch_profile` already has exactly this check; `fetch_fundamentals`
  is the copy that lost it.

- [x] **L3 — Unvalidated path text is written to the database.**
  `app/api.py:313` (`_valuation_ticker_from_path`) slices the ticker out of the
  **raw** path in the middleware, before FastAPI's `^[A-Za-z][A-Za-z.\-]*$` /
  `max_length=10` route validation runs. A request to `/v1/valuations/<junk>`
  that then 422s still writes that junk into `usage_events.ticker`, bounded only
  by the platform's URL limit. Not injectable (PostgREST parameterizes it), but
  it means the metering table and the `ticker` telemetry field can disagree, and
  it lets a caller choose what goes in a row of our database.

- [x] **L4 — In-process caches have no bound.** `app/fundamentals.py:284`:
  `_cache`, `_raw_cache`, `_profile_cache`, and `_negative` are plain dicts with
  no cap and no sweep — an entry is only ever evicted when *the same ticker* is
  asked for again. The negative cache is the exposed one: a caller spending
  quota on distinct nonexistent tickers adds an entry per ticker for the life of
  the process. Same shape in `app/supabase.py:674` (`_last_used_writes`, one
  entry per key id, forever). Serverless instance churn hides this in production
  today; a long-lived process is what it would bite.

- [x] **L5 — API-key prefixes are interpolated into PostgREST filters
  unvalidated.** `app/supabase.py:113` builds `params={"prefix": f"eq.{prefix}"}`
  where `prefix` is whatever the caller put between the first two underscores of
  `X-API-Key` — never charset-checked (`app/auth.py:121` only checks the
  three-part shape). httpx percent-encodes the value, so a second filter cannot
  be injected **today**; the defense rests entirely on the encoder rather than on
  the input being what it claims to be.

- [x] **L6 — The FMP client interpolates transport-error text; the Finnhub
  client deliberately does not.** `app/providers/fmp.py:177` builds
  `f"transport error: {exc}"` and carries it into the final `ProviderError`
  message. `app/providers/finnhub.py:92` chains instead, with the comment "httpx
  error text can embed the request URL, which carries the token" — and FMP
  authenticates with `?apikey=` in the query string, which is precisely the same
  exposure. The message never reaches a customer (503s carry fixed text), so this
  is defense in depth, but the two clients should not disagree about a rule one
  of them wrote down.

- [x] **L7 — No `SECURITY.md`.** A public repository with a live production
  service and no stated reporting channel leaves a finder with nowhere to send
  a report except a public issue — which discloses the bug to everyone the
  moment it is filed. Added at `.github/SECURITY.md` (GitHub only detects a policy at the repo root, `.github/`, or `docs/` — `project-docs/` would be invisible), routed through **private vulnerability
  reporting** so no personal contact address has to be published; scope,
  out-of-scope, and testing guidance are stated so a researcher does not have to
  guess. *(Found while writing it: the repo's blanket `*.md` ignore rule was
  swallowing the file — GitHub only offers "Report a vulnerability" when it is
  actually committed. `.gitignore` now exempts it. One owner step remains: the
  feature must be switched on in **Settings → Security**, `TODO.md` §8.)*

- [ ] **L8 — The app emits no HSTS of its own.** *(Verified live: Vercel injects
  `Strict-Transport-Security: max-age=63072000`, so **production is covered** —
  this is informational.)* It is worth knowing that the header is the platform's,
  not the application's: the same code served anywhere else has no HSTS, and the
  Vercel default carries neither `includeSubDomains` nor `preload`.

- [ ] **L9 — `connect-src` allows `http://127.0.0.1:*` and `http://localhost:*`
  from the production page.** Deliberate — it is what lets the `/dcf` endpoint
  builder call a developer's local server — and it is also the reason a pasted
  API key cannot be exfiltrated to a third-party host by that page. Recorded as
  an accepted trade-off, not a defect.

---

### Verification of the fixes

Everything above marked `[x]` was implemented in this session and is covered by
a regression test that fails against the old code.

- **Suite 461 → 492 passing, 94.68% coverage**; ruff lint + format and mypy
  clean; the OpenAPI contract snapshot was regenerated deliberately after
  confirming the diff was **exactly one field** (`SensitivityOut.description`,
  which now documents the wider set of null cells).
- **H1 re-checked through the real HTTP route**, not just the engine: every
  boundary case that used to 422 now returns **200** with a partially-filled
  grid (3 of 9 corners null), the ordinary case returns a full 9/9 grid, and
  `wacc=0.6` — genuinely outside the bounds — still returns 422
  `invalid_assumptions`. Swallowing per-cell errors did not swallow real ones.
- **H2 re-checked end to end**: a non-ASCII token in the header, in the cookie,
  or in both now returns **403 `csrf_failed`** carrying `Cache-Control:
  no-store`, `X-Request-ID`, and the security headers — the contract the
  escaping `TypeError` used to skip.
- **M2 is asserted as a measurement**, not a description: the test pins the
  exact upstream call list per request, so a future change that reintroduces a
  wasted auth round trip fails a test rather than quietly costing money.

Two findings were left open on purpose (**M5**, **L8/L9** are informational);
both are in `TODO.md` §8 with the reason. One proposed fix was **written and
then rejected** — see M2 — because a test showed it would sign real users out.

---

### Not defects, but worth knowing

- **`API_KEY_HASH_PEPPER` unset is not a weakness here.** Keys carry 32 bytes
  from `secrets.token_urlsafe`, so an unpeppered SHA-256 is not brute-forceable;
  the pepper defends against a stolen-hash-table + weak-key scenario that does
  not apply to generated keys.
- **Timing on API-key lookup** distinguishes "unknown prefix" (returns before
  hashing) from "known prefix, wrong secret". Both paths make one Supabase round
  trip, so network noise dominates the hash comparison by orders of magnitude.
- **`/openapi.json`, `/health`, and `/ready` are unauthenticated by design.**
  `/ready` reports names and statuses only, caches for 5 s, and collapses
  concurrent probes onto one check, so it cannot be used to amplify into the
  database.

## ⚠️ Live production defects found 2026-07-26 (doc/state audit)

Both were found by reading the real production runtime logs (Vercel MCP,
`pub-tools-dcf` production, deployment `dpl_9gYqAHSc…` = commit `912c9de`).
Neither is visible from tests, which is why they survived a green suite.

- [x] **The production `FINNHUB_API_KEY` value is not a valid key — every
  production valuation is returning a null price.** **Corrected 2026-07-26**
  (see the resolution note at the end of this item). A real keyed
  `GET /v1/valuations/AAPL` on 2026-07-26 19:25 UTC logged
  `GET https://finnhub.io/api/v1/quote?symbol=AAPL&token=X-Finnhub-Token
  "HTTP/1.1 401 Unauthorized"` and closed with `"price": "unavailable"`. So
  ADR-008's headline feature — the live market price — is silently off in
  production, and the null-price degrade (working exactly as designed) is what
  hides it. Evidence it is the *value*, not the key or the code: the key in the
  local `.env` (40 characters) returns **HTTP 200** for the same symbol from
  this machine, and the variable *is* present in Vercel for Preview +
  Production (`vercel env ls`, added 9 days ago). The logged token reads
  `X-Finnhub-Token`, which is the name of Finnhub's auth *header* — consistent
  with the header name having been pasted where the key value belongs. **Fix:**
  Vercel → `pub-tools-dcf` → Settings → Environment Variables → replace
  `FINNHUB_API_KEY` (Production, and check Preview) with the 40-character key
  from the Finnhub dashboard, then redeploy. Confirm with one valuation whose
  response carries a non-null `current_price`. Also in `TODO.md` §6.

  **Resolution (2026-07-26).** The owner confirmed the variable was set in both
  `.env` and Vercel — which was true and never in dispute; the finding was that
  the *stored value* differed from the working one. An attempt to settle it by
  diffing the values failed the same way as before:
  `vercel env pull --environment=production` writes `FINNHUB_API_KEY` with an
  **empty** value (the CLI reports the type as *Sensitive* on write), so no pull
  can read it. **The 401 is therefore the proof, not a value comparison** — and
  it reproduced on a second reading of the logs. Two supporting facts: the local
  key returned **HTTP 200** for the same symbol at the same moment, and the
  transmitted token was the 15-character literal `X-Finnhub-Token`, which
  `grep` confirms appears nowhere in the code, `.env.example`, or docs — so it
  was not a fallback the app supplied.
  **Fixed with the owner's go-ahead** via `vercel env rm` + `vercel env add`
  using the working key. Note that removing the Production entry also removed
  the **Preview** one — they were a single shared record — so Preview was
  re-added with the correct value; it had been serving null prices too.
  Production was redeployed **from the same source**
  (`vercel redeploy dpl_9gYqAHSc… --target production`) so that no unreviewed
  working-tree code shipped alongside a credential change; it re-aliased to
  `www.ashaat.dev` and `/health` reported a new instance. **Final confirmation
  is owner-side** (`TODO.md` §6.2): valuations require a customer API key, which
  Claude does not have, so one signed-in request is what closes this.
  ⚠️ **One correction along the way.** The first redeploy targeted the
  deployment id Claude had been reading logs from (`912c9de`), by which time the
  owner had pushed `b068a96` — so it **re-aliased production to the older
  source**, reverting the log-scrub fix in production. Corrected by rebuilding
  `b068a96` (a rebuild, not a `promote`: env vars are snapshotted per deployment
  at build time, so promoting the existing `b068a96` deployment would have
  restored the code while reinstating the broken key). Production now runs
  `b068a96` with the corrected key, instance `d1721502`. See `TODO.md` §7 for
  the two reusable lessons.

  ✅ **Confirmed live 2026-07-27 01:18:55 UTC.** The owner ran one valuation on
  `www.ashaat.dev/dcf`. Production logged
  `GET https://finnhub.io/api/v1/quote?symbol=AAPL&token=REDACTED "HTTP/1.1 200
  OK"` with `"price": "live"`, `finnhub_calls: 1`, `t_price_ms: 63.98` — a 200
  where the same call returned 401 six hours earlier. **This box is closed.**
  *(An earlier attempt the owner reported had not reached production at all: the
  retained log window showed only portfolio page loads, no `/dcf` and no
  `/v1/valuations/*`. The likely cause was a tab pointed at a local server — the
  page derives its endpoint from `window.location.origin`. Worth remembering,
  because it is the same failure shape as the 2026-07-13 sign-in incident: a
  local server silently answering what looks like a production test.)*
- [x] **The `httpx` INFO log prints full outbound request URLs, and the FMP key
  rides in the URL query string — so `FMP_API_KEY` is written to production
  logs in the clear.** **Fixed 2026-07-26** (same session, details below the
  original description). `configure_logging()` installs the scrubbing handler on
  the **`app`** logger tree only (`app/observability.py`, `logging.getLogger("app")`
  + `propagate = False`), which is correct for our own lines but leaves the
  `httpx` logger untouched; on Vercel those records still reach stdout. Proven
  by the Finnhub line above (a credential printed verbatim). FMP builds
  `{"symbol": ticker, "apikey": self._api_key, …}` as query params
  (`app/providers/fmp.py:167`), so **every** FMP call — 4 per nightly refresh,
  plus any cold ticker — emits the key. Supabase and Upstash are unaffected
  (their credentials are headers). This contradicts the Phase 11 Slice 2 claim
  that "secrets and PII never reach a log", which holds only inside the `app`
  tree. **Fix (code, not owner):** either silence/raise the level of the
  `httpx` logger at startup, or attach a scrubbing filter to it — the existing
  `scrub()`/`redact_url()` already do the work; the gap is only which logger
  they are wired to. Rotate `FMP_API_KEY` afterwards, since the current value
  has been in log storage.

  **Fix, as shipped (2026-07-26).** `ScrubbingFilter` in `app/observability.py`,
  attached by `configure_logging()` to the `httpx` and `httpcore` loggers.
  Filtering, not silencing: a logger-level filter runs *before* propagation to
  whatever handler the runtime installed, which is the only hook available for a
  logger this app does not own. **Silencing `httpx` was rejected** — those lines
  are what exposed both defects in this section, so the fix redacts them rather
  than throwing away the visibility. Three details that made it non-trivial:
  (1) the URL arrives as an `httpx.URL` in `record.args`, not in `record.msg`,
  so scrubbing the message alone would have caught nothing; (2) `record.args`
  must stay a **tuple** or `%`-formatting breaks; (3) arguments are only
  replaced when scrubbing actually changed the text, so the `int` behind the
  `%d` status code stays an `int` — otherwise the record fails to format and the
  whole line is lost, which is how a redaction fix turns into an outage in the
  logs. The filter is idempotent across repeated `configure_logging()` calls
  (marked and removed like the handler), and it touches nothing else about those
  loggers — no level, no handler, no `propagate`, so an embedder still decides
  whether httpx logs at all.
  **Tests: 4 new** in `tests/test_observability.py` (suite **457 → 461**, 94.88%
  coverage; ruff/format/mypy clean) — a real `FMPClient.fetch_fundamentals` call
  whose key must not appear in any emitted line while `symbol=AAPL` and the
  status still do, the Finnhub `token=` shape with its `401` preserved, the
  non-string-argument guard, and filter-stacking.
  **Live-verified outside pytest** against real outbound calls, reproducing
  Vercel's stdout handler at INFO — before: `…&token=fake-key-DO-NOT-USE-1234567890
  "HTTP/1.1 401 Unauthorized"`; after: `…&token=REDACTED "HTTP/1.1 401
  Unauthorized"`; and the FMP shape as
  `…income-statement?symbol=AAPL&apikey=REDACTED&limit=8`. *(The verification
  script deliberately avoided importing `app.api`, because that path calls
  `load_dotenv()` and would restore real credentials — the trap recorded in
  `PROGRESS.md` on 2026-07-26.)*
  ✅ **Confirmed in production 2026-07-27 01:18:55 UTC**, on the same log line
  that closed the Finnhub item above: `token=REDACTED` where the 19:25 line had
  printed the credential verbatim. Deployment `dpl_AuLcbsNjps…` (`b068a96`).
  Redaction and diagnostics both intact — `symbol=AAPL`, the `200 OK`, and every
  telemetry field survived.
  ⚠️ **Still owner-side: rotate `FMP_API_KEY`** — the old value was written to
  Vercel's log storage before this shipped, and no code fix can un-log it.
  `TODO.md` §7.3. The FMP path itself has not yet been *observed* redacted (no
  cold ticker or nightly refresh has run since the deploy), but it is the same
  filter on the same logger — and the local live-verification covered the
  `apikey=` shape directly.

# Production sign-in investigation (2026-07-13)

Symptom reported: signing in from the production URL only works when the app
is also running locally.

Diagnosis evidence (all verified live on 2026-07-13):

- The real production deployment is healthy at
  `https://pub-tools-dcf-nu.vercel.app` (`/health` returns 200; Vercel
  auto-assigned the `-nu` suffix because `pub-tools-dcf.vercel.app` was
  already taken by another account).
- `https://pubtools-dcf.vercel.app` — the domain recorded in
  IMPLEMENTATION_PLAN.md and stored in Vercel's `PUBLIC_BASE_URL` — serves
  **no deployment at all** (`DEPLOYMENT_NOT_FOUND`).
- Production `GET /v1/auth/github/login` 302s to Supabase with
  `redirect_to=https://pubtools-dcf.vercel.app/v1/auth/callback` — i.e. after
  GitHub/magic-link auth, Supabase sends the browser to the dead domain (or,
  when that URL isn't in Supabase's redirect allowlist, falls back to the
  Site URL — presumably `http://127.0.0.1:8000`, which is exactly why sign-in
  only completes when the local server happens to be running).

## To fix now (this session)

- [x] **Vercel `PUBLIC_BASE_URL` points at the dead domain.** Root cause of
  the reported symptom. Fixed 2026-07-13 (user-approved): Production value
  replaced with `https://pub-tools-dcf-nu.vercel.app` via `vercel env
  rm`/`add`, docs fix committed (`ba3b93b`) and pushed so the git
  integration redeployed production, and the live login redirect re-verified
  — `redirect_to` now points at the real domain. NOTE: sign-in is not fully
  fixed until the Supabase URL-configuration item below is done.
- [x] **`docs/index.html` still uses the placeholder base URL
  `https://api.example.com`** (2 occurrences) although a real production host
  exists. Swapped to `https://pub-tools-dcf-nu.vercel.app`; also removed the
  leftover half-deleted HTML comment fragment from the old "preview, not yet
  enforced" note. Fixed 2026-07-13, commit `ba3b93b`, verified live.
- [x] **IMPLEMENTATION_PLAN.md records the wrong production domain**
  (`pubtools-dcf.vercel.app`). Corrected with an explanatory note, and the
  stale "Supabase env vars in Vercel unconfirmed" bullet updated (confirmed
  present in Production via `vercel env ls`). PROGRESS.md never contained
  the wrong domain. Fixed 2026-07-13.

## Account-page actions unresponsive (reported 2026-07-13, after sign-in fix)

Symptom: signed in with GitHub on production, but "Generate key", "Revoke",
and "Sign out" buttons do nothing at all — no error, no network activity.

- [x] **`csrfHeaders()` is defined in the wrong `<script>` block in
  `docs/index.html`.** Fixed 2026-07-13, commit `162f002` — helpers moved
  into the account block (both blocks syntax-checked, all 6 call sites now
  in scope), deployed and verified live on production HTML. The page has two separate IIFEs in two separate
  `<script>` tags: the endpoint-builder block (lines ~1226–1497) and the
  account block (lines ~1499–1728). `cookieValue()`/`csrfHeaders()` were
  added to the *builder* block (where nothing uses them), while every
  consumer — `createKey`, `revokeKey`, `rotateKey`, `signOut`,
  `sendEmailLogin` — lives in the *account* block, a different function
  scope. Each button click throws `ReferenceError: csrfHeaders is not
  defined` inside an async handler → unhandled promise rejection → silent
  no-op. Introduced with the CSRF work in the "Phase 7 push" bundle; never
  caught because pytest sends `X-CSRF-Token` directly without executing the
  page's JS, and the last real-browser test of these buttons predates the
  CSRF change. Server-side CSRF verified working on production via curl
  (matching token → 200, wrong token → 403), so the fix is purely moving the
  two helpers into the account block.
- [x] **Silent-failure hardening (same fix):** `revokeKey` ignores a non-OK
  response and `signOut` reloads unconditionally, so any future server-side
  failure of these actions is also invisible. Surface an error message on
  failure like `createKey`/`rotateKey` already do. Fixed 2026-07-13, commit
  `162f002` — both now show an error banner on a non-OK response.

## Needs the project owner (Supabase dashboard / decisions)

- [x] **Supabase Auth URL configuration must match the real domain.** Closed
  2026-07-26 as **superseded**: this item was written for the
  `pub-tools-dcf-nu.vercel.app` host, which the Phase 9 migration to
  `ashaat.dev` replaced. The live equivalents were completed by the owner —
  `PUBLIC_BASE_URL = https://ashaat.dev` and the matching Supabase redirect
  allowlist entry (`TODO.md` §1.3/§1.4) — and sign-in has since been observed
  completing in production. Original wording below for history. In
  Supabase → Authentication → URL Configuration:
  - Add `https://pub-tools-dcf-nu.vercel.app/v1/auth/callback` to the
    Redirect URLs allowlist (the allowlisted URL from Phase 6 used the dead
    `pubtools-dcf.vercel.app` domain).
  - Review the Site URL: if it is `http://127.0.0.1:8000`, failed-allowlist
    fallbacks land on localhost — set it to the production origin once local
    development no longer depends on it (local logins use the explicit
    allowlisted localhost redirect, not Site URL).
  - Then test a full GitHub + email sign-in from the production site with the
    local server STOPPED, including returning after a browser restart.
- [ ] **Auth/quota env vars exist only in the Production environment.**
  `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `PUBLIC_BASE_URL` are not
  set for Preview, so preview deployments run with auth OFF (valuations
  unauthenticated behind the in-process limiter) and sign-in unconfigured.
  Decide whether previews should mirror production auth; if so, add the vars
  for Preview with a preview-appropriate `PUBLIC_BASE_URL`.
  *(Re-confirmed 2026-07-26 via `vercel env ls production`: all three are still
  Production-only. Tracked for the owner as `TODO.md` §5.2 — this is the same
  item, not a second one.)*
- [ ] **Custom SMTP in Supabase** before relying on email magic-link login
  for real customer volume (carried over from Phase 6 — default sender is
  rate-limited/dev-oriented). *(Same item as `TODO.md` §5.1.)*

## Pre-existing findings from the 2026-07-13 plan-vs-code audit

- [x] **Phase B quota consume result is ignored** in `app/api.py` — an
  over-limit race can still receive a 200. Fixed 2026-07-13, commit
  `045d202` — a rejected consume now replaces the computed response with the
  same 429 the pre-flight gate returns; regression test
  `test_stale_peek_does_not_let_an_over_limit_consume_serve_a_200`.
- [x] **Middleware pre-flight errors (401/403 auth, 503 quota-storage) lack
  `Cache-Control: no-store`**. Fixed 2026-07-13, same commit `045d202` —
  both shared response builders now set `no-store` unconditionally (live
  curl had shown Vercel injecting its own `public, max-age=0` default on
  those responses).
- [x] **Login rate limiter is instance-local on Vercel** (in-process
  `DailyRequestLimiter`), so the per-IP login cap is a warm-instance guard
  only in production. **Update 2026-07-13:** fix designed into Phase 8
  (Redis `INCR` per ip+day, Slice B, fail-open to the in-process limiter).
  **Fixed 2026-07-14** — Slice B shipped `RedisLoginRateLimiter`; the
  counter is shared across instances whenever Redis is configured
  (cross-instance test in `tests/test_response_cache.py`).
- [x] **PROGRESS.md missing a session entry** for the CSRF token system,
  peppered API-key hashing, and doc clarifications from the "Phase 7 push"
  commit. Backfilled 2026-07-13 into the audit-session entry; stale
  test/coverage counts corrected; the leftover HTML comment fragment was
  removed in commit `ba3b93b`.

## Phase 8 (designed 2026-07-13; Slice A implemented)

- [x] **User provisioning needed before live verification:** Vercel
  dashboard → `pub-tools-dcf` → Storage → Marketplace → Upstash (Redis),
  free tier, region iad1, connect for Production + Preview. User confirmed
  this is complete 2026-07-13. Local `vercel env pull` and deployment/live
  verification remain.
- [x] **Slice A — distributed fundamentals cache and single-flight.** Upstash
  REST client, four L2 ticker caches, versioned envelopes, token-safe lock,
  fail-open behavior, and shared-backend tests completed 2026-07-13. The
  planned lock TTL was corrected from 10s to 45s to cover the actual bounded
  FMP retry path; losers still fall through after 3s.
- [x] **Slice B — response cache and distributed login limiter.** This closes
  the instance-local login-limiter issue above. Completed 2026-07-14:
  `app/response_cache.py` + route integration (hits skip FMP and
  `compute_dcf`, identical ETag, still metered; generation-aware key ready
  for Slice C's rotation) and `RedisLoginRateLimiter`. 292 tests passing,
  94.04% coverage.
- [x] **Slice C — DB read-through plus immutable normalized snapshots and
  migration 003.** *(Closed 2026-07-26: the scheduler has been observed running
  successfully in production for five consecutive days — details below.)* Added 2026-07-13: cache miss order is L1 → Redis → latest
  verified Supabase snapshot → provider; provider success writes the immutable
  snapshot/current ticker head before publishing cache and returning. See
  ADR-006 and Phase 8 for freshness and database-failure behavior.
  **Part 1 implemented 2026-07-18** (migration 003 drafted + read-through +
  snapshot writes + fail-closed 503 + 20 tests; details in PROGRESS.md);
  the daily-refresh scheduler below is what keeps this box open. ⚠️ Apply
  migration 003 before deploying this code — with Supabase configured, a
  missing table is a storage error (503 on cold tickers), not a miss.
- [x] **Daily all-database-ticker refresh scheduler.** *(Closed 2026-07-26 by
  reading the production ledger: runs for 2026-07-21…2026-07-25 all `succeeded`,
  one claim per (ticker, date), `total=attempted=succeeded=1, failed=0`, and
  `normalized_snapshots` still holds exactly one row — an unchanged filing
  re-confirmed daily updates the head and creates no new immutable row. The two
  UTC schedules produced exactly one claimed run per Eastern date.)* ADR-007 requires one
  complete statements/profile FMP cycle (no quote — ADR-008) for every ticker
  in `ticker_snapshot_heads` during each day's 6 PM Eastern window.
  **Code implemented 2026-07-18** (`app/refresh.py`, the CRON_SECRET-protected
  endpoint, `vercel.json` at `0 22 * * *` + `0 23 * * *`, the
  `America/New_York` hour guard, durable Eastern-date run claim, full
  per-ticker claim manifest with no filter/skip/deferral — details in
  PROGRESS.md). Box stays open until it runs live: needs migration 003
  applied, `CRON_SECRET` in Vercel Production (TODO §4.2), the capacity gate
  below, and a deployed observation of one real run.
- [x] **Daily refresh capacity gate.** *(Verified 2026-07-20 by calculation and
  confirmed 2026-07-26 by five real runs: the one-ticker manifest completes in
  about one second and spends 4 FMP calls, far inside the ~250/day allowance and
  the function duration. Re-run this gate before growing the manifest.)*
  Original wording: before deployment, prove the FMP plan and
  runtime can process the entire ticker manifest (including multiple endpoints
  and bounded retries). Insufficient capacity must fail visibly and trigger an
  upgrade/durable-worker decision; it must not silently omit tickers.

## Phase 9 — Public site: domain migration (owner actions, 2026-07-16)

Code for Phase 9 Slices 1–2 is **done** (portfolio at `/`, API directory at
`/apis`, DCF at `/dcf`, images at `/Pics/*`, auth callback retargeted to
`/dcf`). What remains needs the owner's Vercel/Supabase dashboards. **Do these
in order — skipping any of the middle three breaks sign-in in production**,
exactly as the 2026-07-13 `PUBLIC_BASE_URL` incident above did.

**The domain is `ashaat.dev`** (confirmed by the owner 2026-07-16).

> **Status sync 2026-07-26.** These boxes had gone stale against `TODO.md` §1,
> which is the owner's live queue and records the work as done. Corrected here
> so the two files stop contradicting each other. What is genuinely still open
> is only §1.4c (apex is not the primary host) and §1.5 (a sign-in round trip
> tested with the local server stopped).

- [x] **Remove `ashaat.dev` from the standalone portfolio Vercel project**
  (a domain can only be attached to one project at a time). Done by the owner
  (`TODO.md` §1.1).
- [x] **Add `ashaat.dev` to the `pub-tools-dcf` project** and verify DNS
  resolves. Done (`TODO.md` §1.2). ⚠️ The www/apex direction was *not* settled
  the way the owner intended — see §1.4c and the note below.
- [x] **Update Production `PUBLIC_BASE_URL`** to `https://ashaat.dev` and
  redeploy. Done 2026-07-16 (`TODO.md` §1.3); the login redirect was
  live-verified emitting `redirect_to=https://ashaat.dev/v1/auth/callback`.
- [x] **Add `https://ashaat.dev/v1/auth/callback`** to Supabase →
  Authentication → URL Configuration → Redirect URLs, and review the Site URL.
  Done by the owner (`TODO.md` §1.4). Supabase only honors allow-listed
  redirect targets. (The previously allow-listed `pub-tools-dcf-nu.vercel.app`
  callback can stay — the vercel.app host keeps working — but
  `PUBLIC_BASE_URL` decides which host sign-in actually returns to, so only one
  completes a round trip.)
- [ ] **⚠️ Flip the primary domain to the apex** (`TODO.md` §1.4c).
  Re-verified live 2026-07-26: `https://ashaat.dev/` still returns **308 →
  `https://www.ashaat.dev/`**, i.e. www is still primary, the reverse of the
  stated intent. It works only because the 308 preserves `?code=`, and the
  PKCE `pt_oauth_verifier` cookie is host-only.
- [x] **Update the hardcoded base URL in `docs/index.html`** →
  `https://ashaat.dev`. Done 2026-07-16: both curl examples updated. The
  endpoint builder needed no change — `defaultBaseUrl()` derives from
  `window.location.origin`, so it follows whatever host serves the page.
- [ ] **Test on `https://ashaat.dev` with the local server stopped:** `/`
  portfolio, `/apis` directory, `/dcf` tool, a full GitHub **and** email sign-in
  round trip landing on `/dcf`, and a valuation request. *(Still open as
  `TODO.md` §1.5, though production logs on 2026-07-26 show a real browser
  completing `/v1/auth/me`, key create/revoke, and a 200 valuation — so the
  round trip does work; what is missing is the owner's deliberate
  local-server-stopped confirmation.)*
- [x] **Retire the old portfolio Vercel project** once `ashaat.dev` resolves
  here. Done (`TODO.md` §1.6); `vercel list-projects` shows `pub-tools-dcf` as
  the only project on the account (verified 2026-07-26).

Notes for whoever does this:

- The `-nu` vercel.app domain keeps working alongside the custom domain; only
  `PUBLIC_BASE_URL` decides where sign-in returns to, so sign-in will complete on
  exactly one host until the custom domain is configured everywhere.
- `docs/Pics/` (~540 KB, 7 images) now ships in the function bundle. If Vercel
  bundle size ever becomes a gate, move these to Vercel static hosting/CDN
  rather than serving them through the Python function.
- 4 images in the source portfolio repo were **not** copied because nothing
  referenced them (`AWS-Certified-Cloud-Practitioner-logo.png`, `GithubLogo.png`,
  `Java_programming_language_logo.svg.png`, `OutlookLogo.jpg`).
  **Updated 2026-07-17 (`TODO.md` §2.3):** the AWS logo is now in use — a
  Certified Cloud Practitioner card was added to the certifications section, and
  production logs confirm `/Pics/AWS-Certified-Cloud-Practitioner-logo.png`
  serving 200. The other three stay skipped (two are non-transparent duplicates,
  one has no section). **Still owner-side:** supply the certification's earned
  date (or "in progress") so the card can show it — no date was invented.
- The source portfolio repo also has a second page (`portfolio.html`, "Course
  Portfolio") that was **not** migrated; its nav link was dropped. **Decided
  2026-07-17 (`TODO.md` §2.1): drop it.** Nothing further to do.

## Feature: real-time current price from Finnhub, never cached (planned 2026-07-16)

Recorded as **ADR-008** in `ARCHITECTURE_DECISIONS.md`. This section is the
working definition and the sliced implementation plan.

### What and why

Today the market price reaches the response through two cache layers — the
per-ticker quote cache (`FundamentalsService._get_quote`, 60s L1 / Redis
`quote:`) and the whole-response valuation cache (`dcf:v1:resp:`, 60s) — and the
planned ADR-007 daily refresh would make it up to a full day old. The product
requirement is that the response always shows a **live** price.

**User decision (2026-07-16):** the current price must be requested from Finnhub
on every call, and **no current price may be cached anywhere**.

**Clarified (2026-07-16):** the *only* caching wanted is the **financial
statements** for widely-used tickers. If a customer runs three different discounts
on AAPL, the statements must be fetched from FMP **once**, not three times — but
the request/response and the DCF math are **not** cached. The math is recomputed
per request (it is pure and cheap) from the cached statements + the live price.
This retires the Phase 8 Slice B valuation response cache (`dcf:v1:resp:`) and the
Phase 7 ETag/304 handling for this endpoint; the Slice A fundamentals cache
(`fund:`/`profile:` + single-flight) is exactly the "reuse the statements"
mechanism and is kept.

### Customer-request read path (confirmed 2026-07-16)

Every valuation request resolves the **statements** and the **price** on two
independent paths, then computes fresh:

1. **Statements** — cache → database → FMP, returning as soon as one layer has
   them (ADR-006 read-through; the detailed order lives in `IMPLEMENTATION_PLAN.md`
   Phase 8 "Authoritative cache-aside read/write flow"):
   - in-process L1 cache hit → use it;
   - else Redis L2 (`fund:`/`profile:`) hit → hydrate L1, use it;
   - else Supabase snapshot (DB) hit → hydrate cache, use it;
   - else (genuinely cold ticker, absent everywhere) → fetch from FMP, persist to
     DB + cache, use it.
   - A ticker already in the DB is **never** refreshed from FMP by a customer
     request, even when stale; the ADR-007 daily 6 PM job is the only refresher
     (user decision 2026-07-16). Stale DB data is served with a freshness note.
2. **Price** — always live from Finnhub, cached nowhere (ADR-008).
3. **Compute** — run the DCF fresh from the resolved statements + the live price;
   the response is not cached.

**Dependency/sequencing:** the database step (cache → **DB** → FMP) is Phase 8
**Slice C**, which is not yet implemented — today's code does cache → FMP with no
DB layer. This Finnhub feature (live price + retiring the response cache) is
independent of Slice C and can ship first; the DB layer then slots into the
statements path without changing the price path.

### Scope

- In scope: a Finnhub quote client; live per-request price fetch; injecting
  `current_price`/`price_as_of`/`price_fetched_at`/`upside_pct` fresh on every
  request; stripping those four from the response cache; removing the FMP quote
  path; making `/v1/valuations/*` `no-store`; docs/config/tests.
- Out of scope: changing the FMP statements/profile path (ADR-006/007 unchanged
  except that the daily cycle no longer fetches a quote); Finnhub for anything
  other than price; websocket/streaming (REST `/quote` is enough).

### Key architectural consequences (accept explicitly)

1. **`/v1/valuations/*` becomes `Cache-Control: no-store`.** The Phase 7 public/
   edge cache no longer applies to this endpoint because the body carries an
   uncacheable live price and the response is no longer cached at all. Provider
   savings come from the **financial-statement cache**, not a response cache.
2. **The response cache is retired, not modified.** `app/response_cache.py`
   (`dcf:v1:resp:`, Slice B) and its route integration are removed, along with
   the ETag/conditional-304 handling for this endpoint. Nothing strips price
   fields from a response cache because there is no response cache.
3. **One Finnhub call per valuation request.** Request volume is re-coupled to
   Finnhub's rate limit (free tier: 60 req/min). Capacity/plan is a deployment
   gate, same shape as the FMP capacity gate above. Coalescing genuinely
   concurrent identical in-flight requests into one live fetch is permitted (it
   returns one live result to simultaneous callers; it is not a time-cache) but
   optional — not required for v1.
4. **The financial-statement cache is the point.** Slice A's `fund:`/`profile:`
   L2 + in-process L1 + single-flight are kept unchanged in purpose: N
   differently-assumed valuations of one ticker cost one FMP statement fetch. The
   cached snapshot must be **price-free** (price is live), so `current_price` is
   dropped from the cached `BaseFinancials` snapshot.
5. **The DCF engine stays pure.** It must produce the price-independent math
   without requiring a positive `current_price` (today `compute_dcf` validates
   `current_price > 0` and computes `upside_pct` internally — that moves to an
   API-layer step applied after the live fetch).
6. **Finnhub outage never yields a stale price.** Degrade to `null`
   price/`upside_pct` + a data-quality warning; still return the intrinsic value.

### Cross-doc updates required

- [x] ADR-007 + Phase 8 Slice C: the daily FMP refresh no longer fetches/stores
  a quote; remove the `dcf:v1:quote:` key from the Phase 8 namespace; Slice C
  must not re-introduce a cached quote. Done 2026-07-18: the Phase 8
  supersession note landed 2026-07-16; the remaining stale details inside
  Phase 8 (`quote:`/`resp:` rows in the key table, `last_quote`/
  `quote_verified_at` on the migration-003 head spec, quote references in the
  single-flight/promotion/exit-criteria text) were scrubbed so Slice C is
  implemented from a spec that cannot reintroduce a stored price.
- [x] Phase 7 notes / `docs/index.html` "Caching" section: valuation responses
  are `no-store`; only the internal compute cache is shared; the price is live.
  Done 2026-07-18: supersession note added at the top of Phase 7 in
  `IMPLEMENTATION_PLAN.md`; `docs/index.html`'s section was rewritten as
  "Live price & caching" with the Slice 2–4 work.
- [x] README + `docs/index.html`: document Finnhub as the price source, the
  `FINNHUB_API_KEY` setup, real-time price / never-cached behavior, and the
  Finnhub-outage null-price degrade. Done 2026-07-18 with the Slice 2–4 work.

### Implementation plan (sliced)

**Slice 1 — Finnhub client + normalization (no route wiring).**
- [x] `app/providers/finnhub.py`: `FinnhubConfig.from_env()` (`FINNHUB_API_KEY`);
  `FinnhubClient` over the existing `httpx` dependency (no new runtime dep, same
  pattern as `app/providers/fmp.py`/`app/supabase.py`). `fetch_quote(ticker) ->
  (raw: dict, fetched_at: datetime)` hitting `GET /api/v1/quote?symbol=&token=`.
  Timeout well under the Vercel function budget; classify transport/timeout/
  malformed-JSON/non-200 into `ProviderError`; empty/`c==0` payload (Finnhub's
  "unknown symbol" shape) into `TickerNotFoundError`. Completed 2026-07-17 —
  3s default timeout, no retries (a quote is latency-sensitive; the caller
  degrades to a null price instead), 401/403 → `ProviderAuthError`, and
  transport exceptions are chained rather than interpolated so the token can
  never leak into an error message (deliberate deviation from the FMP client).
- [x] `normalize_finnhub_quote(ticker, raw, fetched_at) -> NormalizedQuote`:
  map `c`→price, `t` (unix seconds)→`price_as_of` (UTC); reject non-finite/≤0
  price via `NormalizationError`, mirroring `normalize_fmp_quote`. Completed
  2026-07-17 — `t == 0` (Finnhub's "no timestamp") maps to `price_as_of=None`.
- [x] Unit tests with a fake `httpx` transport: happy path, unknown symbol,
  timeout/5xx→ProviderError, malformed JSON, non-finite/zero price, `t` mapping,
  and `from_env` on/off (absent key → client not constructed / feature off).
  Completed 2026-07-17 — 25 tests in `tests/test_finnhub.py` (house style:
  sync tests + `asyncio.run`), including a token-never-in-error-text assertion.
  Suite 298 → 323, 94.25% coverage, ruff/format/mypy clean.
- [x] **Live verification against the real Finnhub API** (2026-07-17, key in
  `.env` + Vercel Preview/Production): real AAPL ($333.74) and MSFT ($393.82)
  quotes fetched and normalized with `price_as_of` = that Friday's 4 PM ET
  close (correct after-hours behavior); an unknown symbol's all-zero body was
  classified `TickerNotFoundError`. `tests/conftest.py`'s isolation fixture now
  clears `FINNHUB_API_KEY` so the ambient real key can never flip Slice 2's
  auto-enable on inside tests (the 2026-07-11 Supabase-creds incident class).

**Slices 2+3+4 — implemented together 2026-07-18** (one structural change; the
route rewrite, cache retirement, and docs land as a unit). Resolutions the
owner made before implementation: (a) **quota flow simplified** — one atomic
`check_and_increment` pre-flight replaces the peek/deferred-consume split
(which existed only so 304s could be free) and `X-RateLimit-*` headers return
on all valuation responses (safe again under `no-store`); (b) a **Finnhub
unknown-symbol for an FMP-served ticker degrades like an outage** (null price +
warning, never a 404/502); (c) **`model_version` bumped 0.1.0 → 0.2.0** for the
nullable-price schema change (OpenAPI snapshot regenerated).

- [x] Engine/model split: `BaseFinancials` and `Valuation` carry **no price
  fields at all** (stronger than Optional — a cached snapshot cannot represent
  a price by construction); `compute_dcf` drops the `current_price > 0`
  precondition and its internal upside math; upside is computed in
  `build_valuation_response(quote=...)` from the live quote.
- [x] Route: statements via the fundamentals cache → live Finnhub fetch →
  fresh `compute_dcf` → `Cache-Control: no-store`. Any `ProviderError`/
  `NormalizationError` from Finnhub (outage, rate limit, unknown symbol,
  auth misconfig) degrades to null price + warning.
- [x] `app.state.finnhub` wired in the lifespan (auto-enable on
  `FINNHUB_API_KEY`, injectable `finnhub_client=` for tests, closed on
  shutdown); absent → "not configured" warning.
- [x] Removed: `app/response_cache.py`, `app/http_cache.py` (only the
  valuation route used it), the ETag/304 branch, `Vary`, the peek phase,
  `is_not_modified`, `FundamentalsService._get_quote`/`_quote_cache`/Redis
  `quote:`/stale-quote logic/`quote_ttl_seconds`, the FMP `quote` endpoint
  from `fetch_fundamentals` (cold load is now 4 FMP calls, not 5),
  `FMPClient.fetch_quote`, and `normalize_fmp_quote`.
- [x] Tests: login-limiter tests split into `tests/test_login_rate_limit.py`;
  new ADR-008 route tests (1 FMP statement load vs 3 Finnhub calls across 3
  same-ticker requests, live-price/upside assertions, no-store + no-ETag,
  outage/unknown-symbol/feature-off degrades, conditional headers ignored and
  metered, atomic-consume ordering, quota headers counting down). Suite:
  **291 passing, 94.08% coverage**; ruff/format/mypy clean.
- [x] Docs: README rate-limit/live-price/statement-freshness sections and
  `docs/index.html` "Live price & caching" section rewritten; example response
  and version strings updated to 0.2.0; `base_financials` example is
  price-free.
- [x] Live-verified against real uvicorn with real FMP+Finnhub keys:
  AAPL 200 with live price $333.74, `upside_pct` −76.07, `model 0.2.0`,
  `no-store`, no ETag/Vary, `X-RateLimit-Remaining: 99`, price-free
  `base_financials`; second call refreshed `price_fetched_at`; with
  `FINNHUB_API_KEY` blanked the same request returned identical math with
  null price/upside + the config warning.
- [x] `FINNHUB_API_KEY` present in Vercel Preview+Production (owner, 2026-07-17).

### Open sub-decisions (defaults chosen; change here if desired)

- **Outage behavior:** default = null price + warning (return the math). Flip to
  502-only if the price is considered mandatory.
- **Concurrent-request coalescing of the live fetch:** default = not built for
  v1; add later only if Finnhub rate limits bite.
