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
