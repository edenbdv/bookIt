# Refresh token rotation and revocation (BOOK-78)

The follow-up `jwt-basics.md` §5 kept flagging as missing. That doc's "Honest
current limitation" section put it bluntly: without this, a leaked refresh token
gave an attacker a flat 7 days, *functionally identical to having issued one
plain 7-day access token* — the two-token split lowered the probability of the
worst case but did nothing about its severity. This is the severity half.

## 1. What was actually broken

Two separate defects, and it's worth keeping them apart because they have
different fixes.

**The token was replayable.** `refresh_tokens()` decoded, checked the signature,
`exp` and `type`, and issued a new pair. Nothing recorded that the token had been
spent, so call #1 and call #500 got the same answer. Note this had nothing to do
with the 7-day expiry — shortening `refresh_token_expire_days` would have shrunk
the window but never made a token single-use. **Single-use requires state; no
expiry value produces it.**

**Nothing could be revoked.** There was no `/auth/logout` at all, and there
couldn't be: logging out means the server refusing a token it would otherwise
accept, and the server had nowhere to record that refusal. Same for "sign out
everywhere" and for killing sessions on a password change.

Both come from the same root — auth was entirely stateless — which is exactly
why BOOK-31 deferred this rather than bolting it on.

## 2. One key per session

```
refresh:session:<sid>  →  the jti that is valid right now     TTL = 7 days
refresh:prev:<sid>     →  the jti just spent (§5's grace)     TTL = 10 seconds
refresh:user:<user_id> →  set of that user's live sids        TTL = 7 days
```

Two new claims make this addressable (`security.py`'s `_create_token`): **`jti`**
is this token's own identity, so one specific token can be named; **`sid`** is
the login session it belongs to, so every token in a chain can be killed
together. Without a `jti` a token is indistinguishable from any other with the
same `sub`/`exp`, which is precisely why nothing could be revoked before.

### Why an allowlist, not a blocklist

BOOK-78's description proposed a blocklist — store the `jti`s that have been
revoked, reject anything in it. That detects reuse, but it **cannot respond to
it**, and the reason is worth internalising:

> A blocklist only ever stores tokens that are *dead*. The identity of the token
> that is currently *alive* is never written down anywhere.

Play out a theft with a blocklist. Alice holds `A`; a thief copies it.

| | |
|---|---|
| Thief refreshes with `A` | not blocked → allowed → thief now holds `B` |
| Alice refreshes with `A` | `A` is blocked → rejected. **Theft detected.** |
| Now stop the thief | To kill `B` you need its `jti` — and you never stored it. |

The thief refreshes forever and Alice is the one locked out. Detection with no
possible response. The same gap breaks "log out all devices": to revoke the
token on a lost phone you'd need its `jti`, and the only device that knows it is
the phone.

An allowlist stores what's alive, so killing a session is one `DELETE` — and it
works on tokens nobody is showing us, which is the entire point. It's also
O(active sessions) rather than O(every token ever issued): the session key is
*overwritten* on each rotation, so a client refreshing every 15 minutes for a
week costs one key, not ~700.

**What's stored is an id, never a token.** A `jti` is inert — minting a token from
one needs `secret_key`. If Redis leaks, an allowlist of `jti`s hands over
nothing usable; a store of tokens would hand over every live session. Same
instinct as `hashed_password` rather than the password.

## 3. The three outcomes

All of `rotate()` (`app/auth/token_store.py`), which is deliberately three
branches of plain Redis commands:

| Presented | Outcome |
|---|---|
| the stored `jti` | **rotate** — write the new `jti`; the old one is dead the instant we write |
| the `jti` just spent, within 10s | **grace** — the two-tabs race (§5), not theft. Returns the *current* `jti` |
| any other spent `jti` | **theft** — `DELETE` the session key, killing the chain including whatever the thief holds |
| no session key | **gone** — already logged out, revoked, or expired |

Only the first two return tokens. The other two produce a `401` that is
byte-identical to every other failure — deliberately, because "that token was
already used" would confirm to a thief that the token they hold is genuine. Same
reasoning as `user-enumeration-basics.md`, applied to tokens.

**On theft, the whole session dies, not just the presented token.** That's the
load-bearing choice, and it's what case 3 of the verification script checks: the
*currently valid* token must also stop working. Killing only the replayed token
would leave the thief's live one untouched — detection that costs the attacker
nothing.

## 4. The flow, end to end

### Login

```
POST /auth/login
  router.login          → verify email + password
  service.issue_tokens  → sid = new_id()          ← the session is born here
                          jti = new_id()
                          mint access + refresh, both carrying sid
  token_store.start_session
                        → SET  refresh:session:<sid> = jti   (7d)
                          SADD refresh:user:<uid>    = sid
  ← { access_token, refresh_token }
```

`issue_tokens` is the **only** place a `sid` is created, and `POST /auth/login` is
its only caller. Signup issues no tokens, so a new account gets its first `sid`
on its first login like anyone else.

### Refresh

```
POST /auth/refresh  { refresh_token }
  service._decode_refresh  → signature valid? not expired? type == "refresh"?
                             carries jti + sid?              ← else 401
  users.get_by_id          → user still exists?              ← else 401
  token_store.rotate(sid, presented_jti, new_jti)
```

Note the `sid` is **read out of the presented token**, never regenerated — that's
what keeps one session on one Redis key across an arbitrarily long refresh chain.
Inside `rotate`, a single read decides everything:

```
stored = GET refresh:session:<sid>

stored is None            → None      session gone (logged out / revoked / expired)
stored == presented       → rotate:   SET session = new_jti
                                      SET prev    = presented  (grace TTL)
                                      returns new_jti
presented == GET prev     → grace:    returns stored   ← two tabs; no rotation
otherwise                 → theft:    DEL session, DEL prev
                                      returns None
```

Back in the service, `None` becomes a `401`; otherwise a new pair is minted
carrying the returned `jti`. **That returned value is not always `new_jti`** — in
the grace case it's the session's *current* one, which is precisely what makes
two racing tabs converge on a single token instead of forking the chain.

### Logout

```
POST /auth/logout       decode → DEL session, DEL prev, SREM from user set → 204
POST /auth/logout-all   access token → for each sid in the user's set: DEL → 204
```

`/auth/logout` takes a refresh token (the long-lived half is the one worth
killing); `/auth/logout-all` authenticates with an access token instead, so a
user who has lost a device can trigger it from a session they still hold.

### The through-line

| | |
|---|---|
| `sid` | born at login, constant for the session's whole life |
| `jti` | changes on every rotation |
| the session key | overwritten, never appended — one key per session, not per token |
| every rejection | the same `401`, so a thief learns nothing from the response |
| Redis unreachable | `503` via `router.py`'s `_with_token_store` — fail closed |

## 5. What this deliberately does not do

**Access tokens are not checked against Redis.** They stay valid until they
expire (15 minutes), so logout kills the refresh chain immediately but leaves a
short tail where the access token still works. The alternative is a Redis round
trip on *every authenticated request*. This is the standard tradeoff, and the
reason access-token lifetime is measured in minutes.

**The grace window, and why one is unavoidable.** A single page load fires several
API calls at once; if the access token has just expired they all `401` together
and all reach for `/refresh` with the same token. Under a strict "usable exactly
once" rule, the second one is theft, and a user gets logged out for opening a
page.

Inside one tab that's a client-side problem with a standard client-side fix:
**one refresh in flight at a time**, with the other calls awaiting the same
promise. Most auth libraries do this.

**Across tabs it can't be fixed that way, and this is the case that forces our
hand.** Two tabs are two JS contexts with two separate interceptors sharing one
refresh token out of `localStorage` — same `sid`, same `jti`, neither aware the
other exists. (Tabs do *not* get separate sessions: a `sid` is minted per login,
and both tabs are the same login. Separate `sid`s come from separate logins —
laptop vs. phone, or a different browser.) Coordinating them needs
`BroadcastChannel` or a `localStorage` lock, which no backend can assume.

So the server absorbs it. `refresh:prev:<sid>` remembers the `jti` just spent for
`refresh_reuse_grace_seconds`; presenting that one returns a token carrying the
session's **current** `jti` rather than rotating again, so both tabs converge on
one token instead of forking the chain. Auth0 calls the same mechanism *leeway*.

**It stores only ids, never token pairs.** An earlier draft cached the issued
`{access_token, refresh_token}` so the loser could be handed the winner's exact
response. Re-minting against the current `jti` gets the same result without ever
writing token material to Redis — the property §2 relies on.

The tuning tension is the whole design in one row:

| | Too short (0–1s) | Too long (minutes) |
|---|---|---|
| Effect | Honest clients logged out by ordinary races | A stolen token stays replayable that long |

**What's still not handled:** two refreshes landing inside the same Redis round
trip. Both read the same current `jti` and both rotate. Closing that needs a Lua
compare-and-set, and it was judged not worth it — the realistic tab-to-tab gap is
milliseconds and a Redis round trip is sub-millisecond, so the grace window
already covers the case that actually happens. Same reasoning shape as
`distributed-lock-basics.md`: close the race that occurs, don't build machinery
for the one that doesn't.

**Redis being down fails closed** (`503`, in `router.py`'s `_with_token_store`).
Failing open would silently restore the exact pre-BOOK-78 behaviour this ticket
exists to remove. That's the same call `_enforce_login_rate_limit` makes and the
opposite of `enforce_user_rate_limit`'s fail-open — the split is always "is this
a security boundary or a fairness control?" (`rate-limiting-basics.md`).

**Tokens issued before this shipped are rejected**, since they carry no `jti` or
`sid` and so belong to no session. Every existing session logs in once more.
Waving them through would mean honouring exactly the unrevokable tokens the
ticket set out to eliminate.

**Where the token lives is still the client's problem.** The frontend keeps it in
`localStorage`, which any XSS on the page can read — see
`docs/frontend/auth-session-basics.md` §2, which explains httpOnly cookies and
why moving to them is Epic 11's BFF rather than a small change. Rotation narrows
that exposure from a different angle: a stolen token no longer buys silent
long-term access, because the thief and the real user are now competing for a
single-use token and the second one past the grace window trips reuse detection.
It caps the theft and makes it visible; it doesn't prevent it. Neither rotation
nor an httpOnly cookie stops a script *abusing* the session in place — both
defend against steal-now-use-later — which is why the BFF is still worth doing.

## 6. Verifying it

```bash
docker compose up -d db redis
uv run uvicorn app.main:app --reload
uv run python scripts/verify_refresh_token_rotation.py
```

Rotation needs a *sequence* of calls to demonstrate — one token working exactly
once, then the same token failing — which no single request can show, so this is
a script rather than a curl example. Not pytest, for the same standing reason as
the other `verify_*` scripts (no suite yet; BOOK-61/BOOK-62).

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Rotation | Every refresh spends the token it was given and issues a new one — each is usable exactly once |
| Revocation | A server-side record that lets a token be refused before its `exp` — what makes logout possible at all |
| `jti` | Registered JWT claim (RFC 7519): this token's unique id. Without one, no token can be individually named |
| `sid` | The login session a token belongs to. One per login, which is what makes per-device logout possible |
| Allowlist vs blocklist | Allowlist stores what's *alive*, so it can kill a token nobody is showing it. A blocklist stores what's *dead* and can only reject what a client presents |
| Reuse detection | A signed, unexpired refresh token whose `jti` isn't the stored one is by definition a copy of a spent token → treat as theft |
| Why kill the session | Revoking only the replayed token leaves the thief's *current* one working — detection that costs the attacker nothing |
| Grace window | A just-spent token still answers, briefly, with the session's current one. Needed because two browser tabs share a `sid` and can't coordinate — client-side single-flight only fixes one tab |
| Fail closed | Redis down → `503`, never "accept any signed token" — that fallback is the bug this ticket removed |
