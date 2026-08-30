# Frontend auth session basics (BOOK-84)

## 1. The problem this solves

BOOK-31 issued access and refresh tokens. BOOK-33 built `get_current_user`, so protected routes reject anyone without a valid access token. But between those two lives a gap nothing on the backend can close: **something has to actually put the token on the request, and renew it when it expires.**

`docs/auth/jwt-basics.md` section 5 names this explicitly:

> that "something calls `/auth/refresh` automatically" step is entirely **client-side** logic (e.g. a web/mobile app's HTTP interceptor watching for a `401` and silently retrying through `/auth/refresh`) — it does not exist anywhere in this repo […] this ticket built the *endpoint* a client would call that way, not the client automation itself.

This is that automation. Before it, the frontend stored an access token at login and never sent it — auth *looked* wired up while every request went out anonymous.

## 2. Why the client owns refresh at all

The server is stateless by design (`jwt-basics.md` section 1). On a `401` it has no session to consult and no idea who you were, so it cannot renew anything on your behalf. It hands **both** tokens to whoever logs in, and takes the refresh token back in a request body:

```python
class Token(BaseModel):          # POST /auth/login returns this
    access_token: str
    refresh_token: str

class RefreshRequest(BaseModel):  # POST /auth/refresh accepts this
    refresh_token: str
```

That is an explicit client-refresh contract. Giving a client a refresh token it never uses just means the 15-minute `access_token_expire_minutes` turns into "logged out every 15 minutes."

The alternative is a **BFF** — the browser holds an httpOnly cookie, a server-side component holds the tokens, and the SPA never touches one. That is genuinely more secure (`localStorage` is readable by any XSS on the page) and is on the roadmap as part of Epic 11's gateway/BFF. It would *replace* this code rather than extend it.

### What "httpOnly cookie" actually means

A cookie is a value the browser stores per-domain and **attaches automatically** to every request to that domain — no interceptor, no `Authorization` header, no application code. The server sets one by sending a header back:

```
Set-Cookie: refresh_token=<value>; HttpOnly; Secure; SameSite=Strict; Path=/auth/refresh
```

Three flags do the work, and each answers a different question:

| Flag | What it does |
|---|---|
| `HttpOnly` | The browser refuses to expose this cookie to JavaScript. `document.cookie` simply cannot see it. |
| `Secure` | Only ever sent over HTTPS, never plain HTTP. |
| `SameSite` | Controls whether the browser attaches it on requests originating from *other* sites — the CSRF control. |

**`HttpOnly` is the whole point of the comparison.** `localStorage` is JavaScript-readable by definition — that is what it's *for*, and it's why `configureAuth` in section 3 can hand tokens to `api/client.ts` at all. The consequence is that any script running on the page can read it: not just ours, but anything injected through an XSS hole, a compromised dependency, or a malicious browser extension. An httpOnly cookie removes that read entirely — the token exists in the browser but is unreachable from the page's own code.

**What it protects against: exfiltration.** With `localStorage`, an XSS can read the refresh token and POST it to an attacker's server. From there it works from anywhere, at any time, long after the victim closes the tab.

**What it does not protect against: abuse in place.** If a script is executing on your page, the browser still attaches the cookie to every request that script makes. The attacker can act as the user for as long as the page is open. They just can't take the credential *with* them.

So the honest framing is that httpOnly turns "stolen forever, usable anywhere" into "abusable while you're on the page." That's a large reduction, not immunity.

**Why this can't be a frontend-only switch.** The contract above hands both tokens back in a JSON body and takes the refresh token back in a request body. Cookies invert that: the *server* must send `Set-Cookie` at login and read the cookie on `/auth/refresh` instead of `RefreshRequest`. And because cookies attach automatically, CSRF becomes a live concern that a body-carried token never had — hence `SameSite`, and a narrow `Path` so the refresh cookie isn't sent on every ordinary API call. That's backend work on the auth module's public contract, which is why it sits in Epic 11 rather than being a small change here.

**In the meantime, BOOK-78 narrows the same gap from a different direction.** With rotation (`docs/auth/refresh-token-rotation-basics.md`), a refresh token stolen out of `localStorage` no longer buys silent long-term access: the thief and the real user are now competing to use the same single-use token, and whichever refreshes second past the grace window trips reuse detection and kills the session for both. It doesn't stop the theft — it caps it and makes it *visible*. Note that rotation and httpOnly defend the same thing (steal-now-use-later) and neither one stops a script abusing the session in place, which is why the BFF is still worth doing even now that rotation has shipped.

### The third option: in memory — and why the real answer splits the two tokens

`localStorage` and cookies aren't the only choices. An SPA can hold a token in a plain JS variable or React state and never persist it at all.

| Where | Survives reload | Readable by page scripts |
|---|---|---|
| `localStorage` | yes | yes — one line, `localStorage.getItem(...)` |
| in memory | **no** | yes, but only by a script that hooks into the app |
| httpOnly cookie | yes | **no** |

**In memory is genuinely better at rest.** Nothing is sitting in storage for a generic XSS payload to scrape; an attacker has to reach into a module closure or component state rather than read a well-known key. That's not a hard security boundary — a script running in the same context can still get there — but it's a real step up from a value any drive-by payload can lift in one line. And it leaves nothing behind when the tab closes.

**The blocker is `F5`.** Hold the *refresh* token only in memory and every page reload logs the user out — precisely the persistence requirement that drove `localStorage` in section 3's `hydrate → setItem` flow.

**So the standard pattern splits the two tokens by lifetime rather than picking one home for both:**

| Token | Where | Why |
|---|---|---|
| access | **in memory** | 15 minutes, re-obtainable on demand, so it never needs to survive a reload |
| refresh | **httpOnly cookie** | must survive a reload, and application code never needs to *read* it — only the browser does, automatically |

On page load the SPA calls `/auth/refresh` with no token of its own; the browser attaches the cookie by itself, and a fresh access token lands in memory. Reloads work, and neither token is ever sitting in JS-readable storage. That combination — not the cookie alone — is what Epic 11's BFF is aiming at.

**One thing it does not fix:** the two-tabs refresh race. Each tab still calls `/auth/refresh` independently on load, against the same session, so the server-side grace window described in `refresh-token-rotation-basics.md` §5 stays necessary under any of the three storage choices.

## 3. Wiring: why `configureAuth` instead of reading `localStorage`

`api/client.ts` is a plain module — it cannot read React state. The obvious shortcut, reading `localStorage` directly from the client, would give the session **two owners** that must agree on a key name and a shape forever.

Instead the provider — which already owns the session — registers accessors, and the client just calls them:

```ts
export interface AuthHooks {
  getAccessToken: () => string | null
  refreshAccessToken: () => Promise<string | null>
}
```

The accessors read a **ref**, not the `session` state variable:

```tsx
const sessionRef = useRef<Session | null>(session)
sessionRef.current = session
```

A closure registered once that captured `session` directly would freeze the token from the render that created it and go stale on the very first refresh.

### The ordering trap (a real bug this caused)

Registration happens **during render**, not in a `useEffect`:

```tsx
api.configureAuth(hooksRef.current)
```

React runs **child effects before parent effects**. `Feed` fetches its data in a `useEffect`, so on any full page load with a stored session, `Feed`'s fetch fired *before* `AuthProvider`'s effect had registered anything — and went out with no `Authorization` header. Verified in the browser: the header was present after logging in (that path re-renders after the effect already ran) but absent after a reload, which is exactly the case a returning user hits.

Assigning module state is idempotent, so doing it every render is harmless.

### And why there is no unmount cleanup

The natural instinct — `return () => configureAuth(null)` — actively breaks StrictMode's mount/unmount/remount cycle in dev: the teardown unregisters, effects re-run with **no intervening render** to re-register, and every request in the second pass loses its header. Re-registering inside the effect doesn't rescue it either, because of the same child-before-parent ordering above.

Nothing leaks by omitting it: a replacement provider overwrites the hooks during its own render, and the accessors only read refs.

## 4. Logging in

```
AuthForm submit → useAuth().login(email, password)
  → api.login()            ── anonymous: no token attached
  → POST /auth/login       → { access_token, refresh_token }
  → persist()
       ├─ setSession(...)      → re-render; the header shows the user
       └─ localStorage.setItem → survives a reload
```

Both tokens are stored. Keeping only the access token is what made the earlier implementation unable to renew anything.

## 5. An authenticated request

```
component → api.someCall()
  → token = authHooks.getAccessToken()
  → headers.Authorization = `Bearer ${token}`
  → fetch → 200
```

The header is attached **only when a session exists** — public endpoints must keep working logged out, and an empty `Bearer ` header is worse than none.

Note this applies to public endpoints too: while logged in, `GET /properties` also carries the token. That's harmless (the endpoint ignores it) and simpler than maintaining a list of which routes care.

## 6. The refresh path

Access tokens last 15 minutes, so expiry mid-session is **routine, not exceptional**:

```
fetch → 401
  → 401 AND not anonymous AND we actually sent a token?
  → await refreshAccessToken()
        already refreshing? reuse that promise          ← section 7
        no refresh token?   give up, return null
        POST /auth/refresh { refresh_token }            ← itself anonymous,
        persist(new pair)                                 so it cannot recurse
        return new access token
  → replay the ORIGINAL request with the new token
  → 200
```

The calling component never learns this happened; it just gets its result slightly later.

Two guards matter:

- **`anonymous` on every `/auth/*` call.** Without it, a failed refresh would 401 and trigger… another refresh, forever.
- **Only refresh if a token was actually sent.** A `401` with no token means "log in", not "renew" — retrying would be pointless.

## 7. One in-flight refresh

A page can fire several requests at once. If they all `401`, they share a single refresh rather than firing N identical round-trips and racing each other to write the result:

```tsx
if (inFlightRefresh.current) return inFlightRefresh.current
```

Today this is only an efficiency measure, because refresh tokens are **not single-use** (section 9). It becomes load-bearing the moment BOOK-78 lands: with rotation, two parallel refreshes would spend the same token twice, and the second would look like a **replay — i.e. a theft signal** — to the detection logic BOOK-78 adds.

## 8. When refresh fails

Refresh token expired (7 days) or invalid:

```
api.refreshTokens throws
  → persist(null)   ├─ state cleared → header flips to logged-out
                    └─ localStorage cleared
  → return null → no retry → ApiError(401) reaches the component
```

The session is dropped rather than left in place, so the UI can't keep *looking* logged in with credentials that no longer work.

**No forced redirect to login.** Yanking someone off a public property page because a background token expired is hostile, and there are no protected routes to redirect away from yet. That belongs with the first protected route, not here.

## 9. What is *not* implemented (and isn't this layer's job)

`POST /auth/refresh` mints a new pair but **does not invalidate the refresh token spent to get it**. Verified directly — replaying the same refresh token repeatedly keeps returning `200`:

```
refresh #1 with R1  -> 200
replay same R1      -> 200
replay same R1      -> 200
```

So a refresh token stays valid for its full `refresh_token_expire_days` no matter how many times it's used. That is the standing exposure **BOOK-78** (rotation + revocation, needs Redis for a `jti` blocklist) exists to close. Storing the returned `refresh_token` here is therefore *housekeeping*, not a security boundary — but it's what makes this client rotation-ready without changes.

When BOOK-78 lands, this layer needs essentially nothing: it already stores each returned refresh token and already serialises refreshes. The one likely addition is `logout()` calling a server-side revoke endpoint, since today it only clears local storage.

## 10. Current status

**Nothing in the UI calls an authenticated endpoint yet** — properties, reviews and cities are all public. This is plumbing laid ahead of the booking flow (BOOK-83) that will be its first real consumer.

What has been verified in a browser: the header is attached on every request when a session exists (including after a full reload), absent when logged out, and the refresh token is persisted. What has **not** been exercised end-to-end is the `401 → refresh → replay` path in section 6, because no UI action currently calls a protected route. That should be verified when the booking flow adds one.
