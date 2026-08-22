# JWT basics (BOOK-31)

## 1. The problem: HTTP forgets who you are between requests

BOOK-30 gave us `/auth/login`, which checks an email+password once and confirms "yes, this is a real user." But HTTP is **stateless** — the very next request the same user makes is a brand new, unrelated HTTP request. The server has no memory that a login just happened. So something has to answer, on *every single request* after login: **who is this, and how do we know they actually logged in?**

There are a few ways to solve that:

- **Resend the password every request.** Technically works, but means the password travels over the network constantly (bigger exposure window) and the server re-runs the slow, deliberately-expensive `verify_password` (`password-hashing-basics.md` §5) on every single API call — a non-starter.
- **Server-side sessions.** At login, the server generates a random session id, stores `session_id -> user_id` somewhere (a DB table, Redis), and gives the client just that id (usually via a cookie). Every later request sends the id back, and the server looks it up. This works, but the server now has to *remember* every logged-in user somewhere and check that store on every request — extra infrastructure, and every server instance needs access to the same shared store.
- **A signed token the client holds (JWT).** At login, the server creates a token that already contains "who this is" (a user id) and signs it with a secret only the server knows. The client sends that token back on every request. The server doesn't need to remember anything or look anything up — it just re-checks the signature (section 3) to confirm the token is genuinely one it issued and hasn't been tampered with. This is what "stateless authentication" means: the proof of identity travels *inside* the token itself, not in server-side memory.

That's the actual point of a JWT: it lets the server confirm "this request is really from user X, and this claim hasn't been altered" without a database lookup or shared session store on every request. The trade-off (worth knowing up front, expanded on in the BOOK-31 Jira ticket): since nothing is stored server-side, there's no simple way to invalidate one specific token early — it's valid until `exp`, full stop. That's exactly why access tokens are kept short-lived (section 4) rather than relying on the ability to revoke them.

## 2. What a JWT actually is: three parts, glued with dots

A JWT (JSON Web Token) is a string with three parts separated by dots:

```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjYjc1ZmE1Ny03MjFiLTQzMDEtYmIwOC1hYWE2Y2NjODQ0NmMiLCJ0eXBlIjoiYWNjZXNzIiwiaWF0IjoxNzg3MzM1NDg2LCJleHAiOjE3ODczMzYzODZ9.ykLM_VqhxEv5xfqi82NxkbrOpfM4OaHYRkY6zSO53aw
└──────────────── header ────────────────┘.└──────────────────────────────── payload ─────────────────────────────────┘.└──────────── signature ────────────┘
```

- **Header** — which algorithm signed this (`{"alg": "HS256", "typ": "JWT"}`)
- **Payload** — the actual claims (`sub`, `type`, `iat`, `exp` — section 6)
- **Signature** — proof the payload hasn't been tampered with (section 4)

Each part is [Base64URL](https://en.wikipedia.org/wiki/Base64#URL_applications)-encoded, not encrypted. That distinction matters enough to get its own section.

## 3. Signed, not encrypted — anyone can read the payload

This trips people up: a JWT is not a sealed envelope. Base64URL is **encoding**, not hashing or encryption (recall section 2 of `password-hashing-basics.md`: encoding is reversible by definition). Paste that middle segment into any base64 decoder — no secret key needed — and you get plain JSON back:

```json
{"sub": "cb75fa57-721b-4301-bb08-aaa6ccc8446c", "type": "access", "iat": 1787335486, "exp": 1787336386}
```

So **never put anything secret in a JWT payload** — no password, no internal-only data. The only thing a JWT protects is *integrity* (has this been tampered with?), not *confidentiality* (can anyone read it?). That's exactly why `app/auth/schemas.py`'s payload only carries a user id (`sub`) and a couple of timestamps — nothing sensitive.

## 4. The signature: HMAC with our secret key

If the payload is just readable JSON, what stops someone from editing `"sub"` to a different user's id and using the token as themselves? The **signature** — and this is also the piece that actually delivers on section 1's promise (proving a request is genuinely from a user we authenticated, without a database lookup).

`jwt.encode(payload, settings.secret_key, algorithm="HS256")` runs HMAC-SHA256 over the header+payload, using `secret_key` as a key, and appends the result as the third segment. `jwt.decode(token, settings.secret_key, algorithms=["HS256"])` recomputes that same HMAC from the token's header+payload and checks it matches the third segment. If anyone edits so much as one character of the payload (e.g. swapping the user id), the recomputed signature won't match the original one anymore, and `decode` raises — that's how tampering gets caught even though the payload itself was always readable.

### Signing vs. encryption — both can use a key, but the key does a different job

It's easy to assume "uses a secret key" means "encrypted," since section 3 established encryption as the thing that needs a key. It doesn't, and the difference matters:

- **Encryption**: the key transforms the message *itself* into unreadable ciphertext — without the key, you can't even read the content. The key's job is **hiding** the message.
- **Signing (HMAC, our case)**: the key never touches or hides the message. The payload stays exactly as readable as section 3 showed, key or no key. What the key produces is a **separate, extra value** — the signature — computed *from* the payload, appended *alongside* the still-fully-readable payload. The key's job is **proving authenticity** (this payload came from whoever holds the key, and wasn't altered since), not hiding anything.

So the test isn't "is a key involved" — it's "does the key make the content unreadable, or does it just let you prove something about content that was already readable?"

### What actually happens if an attacker edits the payload

The attacker never has `secret_key` — it lives only on our server, never sent to the client. There are two things they could try, and both fail, for different reasons:

1. **Edit the payload, leave the old signature attached.** The simplest attack: decode the payload, change `"sub"` to a different user's id, re-encode it, keep the *original* signature unchanged, send it. This fails because the signature is computed from **key + message together**. When our server verifies, it recomputes the HMAC using the same real key, but now over the *edited* payload — same key, different message, completely different output (the exact avalanche effect covered for password hashing in `password-hashing-basics.md` §2: changing even one character of the input scrambles the entire output unpredictably). The freshly computed signature won't match the stale one the attacker left attached. **Nothing about the key changed here — it's the changed payload, hashed with the one real key, that breaks the match.**
2. **Edit the payload and also try to compute a matching new signature.** This is the only way to produce a signature the server would actually accept — but it requires `secret_key`, which the attacker doesn't have and can't derive from a decoded token (decoding gets you the payload text; the key itself was never in the token, only its *effect* — the signature — was). Any signature they compute without the real key won't match what the server independently recomputes with it.

Either way, both attempts need the one real `secret_key` to succeed, and neither has it.

**This is different from bcrypt** (`password-hashing-basics.md` §6): bcrypt needs no secret key at all — its security comes purely from being slow. HMAC-signing a JWT *does* need a secret key, because here the goal isn't "make guessing expensive," it's "prove this exact payload came from us and wasn't altered." Only someone holding `secret_key` can produce a signature that `decode` will accept — which is exactly why `config.py`'s comment warns that anyone who learns `secret_key` can forge a valid token for *any* user. Kerckhoffs's principle still holds here too, though: the *algorithm* (HMAC-SHA256) is public and standard; only the *key* is secret — same shape as HMAC's role in that earlier comparison.

### Where this surfaces in code: `TokenError`

Both failed attempts above hit the same wall inside `jwt.decode`: a mismatch between the signature attached to the token and the one it recomputes. PyJWT reports that as `jwt.InvalidSignatureError` — one of several subclasses of `jwt.PyJWTError` (alongside e.g. `ExpiredSignatureError` for a stale `exp`, section 6). `decode_token`'s `except jwt.PyJWTError` catches all of those uniformly and re-raises our own `TokenError` (§8) — so a tampered payload, an expired token, and a malformed token all surface to `service.py` and the router identically, as one generic rejection, the same "collapse distinguishable failures into one" reasoning as `InvalidCredentialsError` in `user-enumeration-basics.md`.

## 5. Access vs. refresh: two tokens, two different lifetimes

One token could theoretically do the whole job, but splitting into two solves a real tension:

- **Access token** — sent on every request to prove who's calling. Short-lived (`access_token_expire_minutes = 15`) *on purpose*: if one leaks (logged somewhere, intercepted, whatever), the damage window is small — it's useless again in 15 minutes. This is also the practical answer to section 1's trade-off (no server-side revocation): we can't cancel a leaked token early, so we cap how long it can possibly matter instead.
- **Refresh token** — used only to obtain a new access token, via `/auth/refresh`. Long-lived (`refresh_token_expire_days = 7`) so the user isn't forced to re-enter their password every 15 minutes; it's needed far less often, so it has fewer chances to leak in the first place.

Short-lived-but-frequent + long-lived-but-rare is the whole point: neither token is both frequently used *and* long-lived, which is the combination that maximizes leak risk.

### Expiring the access token does not mean re-entering a password

This is the part that's easy to miss: without a refresh token, the *only* way to get a new access token after 15 minutes would be a full `/auth/login` again — which needs the real email+password. That forces a bad choice: either store the user's raw password client-side so it can be resent automatically (a serious anti-pattern), or actually make the user retype their password every 15 minutes (unbearable UX, which in practice pressures teams into making the access token long-lived instead — throwing away the whole point of section 1's "small blast radius" design).

The refresh token exists specifically to avoid that choice. It's a separate, longer-lived credential whose only job is minting a new access token, with **no password involved**. So the real flow is: access token expires → something calls `/auth/refresh` with the stored refresh token → gets a new access token → keeps going, with the user never re-entering anything. The user only has to actually log in again once the *refresh* token itself expires (7 days) or is invalid.

**Important scope note:** that "something calls `/auth/refresh` automatically" step is entirely **client-side** logic (e.g. a web/mobile app's HTTP interceptor watching for a `401` and silently retrying through `/auth/refresh`) — it does not exist anywhere in this repo. BookIt is backend-only (`CLAUDE.md`); this ticket built the *endpoint* a client would call that way (`POST /auth/refresh` in `router.py`), not the client automation itself.

**Industry context:** this short-lived-access / long-lived-refresh split is the standard OAuth 2.0 "refresh token grant" pattern, used across most JWT-based auth (Auth0, Firebase Auth, etc.). Two hardening layers common in production systems go beyond what's built here — worth understanding even though neither is implemented yet:

- **Refresh token rotation.** Right now, one refresh token stays valid and reusable for its entire 7-day life — if an attacker steals a copy (e.g. off a compromised device or intercepted traffic) without the legitimate user noticing, that stolen copy works for the rest of the 7 days, indistinguishable from the real user's own use. **Concretely, in this codebase today:** `refresh_tokens()` in `service.py` never marks a refresh token as "used" — its only validity check is `decode_token`'s signature+expiry check (§8), which is stateless and gives the same answer every time. So the *exact same* stolen refresh token can be replayed against `/auth/refresh` as many times as an attacker wants — call #1, #50, #1000 all succeed identically — minting a fresh access token (and a fresh refresh token) on every single call, with nothing ever consumed or invalidated by use. Rotation changes the rule to: every time a refresh token is used, by anyone, the server immediately invalidates it and issues a **brand-new** refresh token (alongside the new access token) in its place — making each one genuinely single-use.

That helps regardless of which side uses the token first:
- **The real user uses their copy first** (which happens naturally, all the time, in the background). Their old refresh token — the same one the attacker is holding a stolen copy of — is now invalidated. The attacker is left holding a dead token.
- **The attacker uses their stolen copy first.** Then when the legitimate client later tries to use what it thinks is still its valid token, the server can see that this exact token was already rotated/used once. That's a strong, detectable signal that theft happened — a legitimate client should only ever be holding the *latest* token it was issued, never one that's already been consumed.

Either way, rotation doesn't prevent the theft itself — it turns "a stolen token stays useful for up to 7 days" into "a stolen token is useful at most once," and gives the server a way to actually notice something went wrong.
- **Revocation/blocklist.** Right now, once a refresh token is issued, nothing can invalidate it early — a user clicking "log out" can't actually kill the token server-side, it just quietly stays valid until its natural 7-day `exp`. Fixing that means giving every token a unique id (the registered `jti` — "JWT ID" — claim, which BookIt doesn't currently set) and keeping a server-side store (Redis, per `core/README.md`) of which `jti`s have been explicitly revoked, checked by `decode_token` on every use.

Both require server-side state tracking token validity, which is exactly what the BOOK-31 ticket flags as out of scope for now ("No revocation/blocklist for refresh tokens yet — would need Redis").

### Future improvement: why rotation/revocation aren't in this ticket

Rotation is a widely-adopted industry best practice (explicitly recommended by the [OAuth 2.0 Security Best Current Practice](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-security-topics), and used by default by Auth0, Okta, AWS Cognito) — but it's a best practice layered *on top of* OAuth2, not something the original spec requires, and plenty of smaller systems ship without it initially.

The reason it's not here yet is architectural, not an oversight: everything else in BOOK-31 — `create_access_token`, `decode_token`, even `refresh_tokens()` itself — is **stateless**, requiring no database or cache lookup to verify a token (just recompute the signature, §8). Rotation and revocation both break that: they need the server to *remember* something about every token it's issued (which one is still "current," or which `jti`s are revoked), which means real added infrastructure (a Redis-backed store, per `core/README.md`), not a small tweak to existing functions. That's a deliberate, larger follow-up, not scope creep into a ticket whose actual goal was getting the stateless issue/verify/refresh mechanism working end-to-end first — which is now done and tested. Tracked separately as [BOOK-78](https://edenbdv.atlassian.net/browse/BOOK-78).

### Honest current limitation: probability, not severity, is what's reduced today

Without rotation/revocation, it's worth being blunt about what the two-token split actually buys *right now*: **if the refresh token itself leaks, the attacker's total access window is a flat 7 days — functionally identical to if we'd simply issued one plain 7-day access token instead of this whole design.** Nothing today shrinks that ceiling once a refresh token is out in the wild; that gap is exactly what BOOK-78 exists to close.

What the split *does* still deliver, even without rotation/revocation, is a reduction in how *likely* that worst case is, not how *severe* it is if it happens: the access token — sent on every single request, and therefore the credential most exposed to interception, logging, proxies, browser extensions — is kept short-lived, so the credential most often exposed has a small blast radius. The refresh token is transmitted rarely (only when refreshing), so it has far fewer chances to leak in the first place. If it never leaks — the common case, precisely because it's sent so infrequently — this design is meaningfully safer than a flat 7-day access token would have been, which would be constantly exposed on every request instead. But if it *does* leak, today's implementation offers no better outcome than that flat 7-day credential would have. In short: **this design lowers the probability of the worst case, not its severity** — severity is what rotation and revocation are for.

## 6. The claims, one at a time

- **`sub`** ("subject") — a *registered* JWT claim (defined by the [JWT spec, RFC 7519](https://datatracker.ietf.org/doc/html/rfc7519#section-4.1.2)), but the spec only says it identifies "the principal that is the subject of the JWT" — generically, *whoever or whatever this token is about*, not specifically "a user." In practice, in essentially every authentication system (BookIt included), that principal is a user, so `sub` ends up holding the user id almost universally — but that's convention, not something the spec forces. `str(user_id)` because JWT claims are JSON, and UUID isn't a native JSON type.
- **`iat`** ("issued at") — when the token was created. Mostly informational/debuggable; not enforced by `decode`.
- **`exp`** ("expiration") — when the token stops being valid. `jwt.decode` checks this **automatically** and raises `jwt.ExpiredSignatureError` (caught by our `except jwt.PyJWTError`) if the token is past it — we never manually compare timestamps.
- **`type`** — not a standard JWT claim, our own addition (`"access"` or `"refresh"`). See section 7 for why it exists.

## 7. Why the `type` claim — an access and refresh token are otherwise identical

Look at `_create_token`: an access token and a refresh token are built by the *exact same function*, signed with the *exact same secret*, and are indistinguishable as JWTs — both just have a `sub`, an `iat`, and an `exp`. Nothing about the token's structure says "I'm only supposed to be used for refreshing."

Without the `type` claim, nothing would stop a client from sending its *access* token to `/auth/refresh` and minting endless new token pairs from a token that was only ever supposed to prove identity for 15 minutes — quietly defeating the entire point of section 5's short-lived-access design. `refresh_tokens()` in `service.py` checks `payload.get("type") != "refresh"` and rejects anything else — verified live: feeding an access token into `/auth/refresh` correctly gets a 401, not a fresh token pair.

## 8. Verifying: same "recompute and compare" shape as password hashing

`decode_token` doesn't "decrypt" the signature back into anything — same shape as `verify_password` (`password-hashing-basics.md` §7): it recomputes the HMAC over the token's header+payload using our known `secret_key`, and compares that to the signature segment already in the token. Match -> the payload is exactly what we signed, and `exp` is checked in the same call. No match, or expired -> `jwt.PyJWTError`, wrapped into our own `TokenError` so `service.py` never has to know it's PyJWT specifically underneath.

```python
# app/auth/security.py
def _create_token(user_id: UUID, token_type: str, expires_delta: timedelta) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "type": token_type, "iat": now, "exp": now + expires_delta}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenError from exc
```

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Stateless auth | Proof of identity travels inside the token itself — no server-side session store to check on every request |
| JWT | `header.payload.signature`, each part Base64URL-encoded — a signed, readable token, not an encrypted one |
| Base64URL | Reversible encoding (like the base64 discussed in `password-hashing-basics.md` §2) — not hashing, not encryption; anyone can decode the payload |
| Signature (HMAC) | Proves the payload wasn't tampered with; requires the secret key to produce or verify, unlike bcrypt which needs no key at all |
| `sub` / `iat` / `exp` | Standard claims: who the token is about, when issued, when it expires (`exp` enforced automatically by `jwt.decode`) |
| `type` claim | Our own addition distinguishing access vs. refresh tokens — without it, either could be used in place of the other |
| Access token | Short-lived, sent on every request — small blast radius if leaked |
| Refresh token | Long-lived, used only to mint a new access token — used rarely, so it leaks rarely |
