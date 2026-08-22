# `get_current_user` basics (BOOK-33)

## 1. The problem this solves

BOOK-31 gave us tokens; BOOK-30 gave us a `User`. But neither one, by itself, answers the question every protected route actually needs answered: **"given this incoming request, who is making it?"** `create_property` (before this ticket) trusted a raw `host_id` query parameter — anyone could type in any UUID and create a listing "as" that user, with zero proof they actually are that user (`property/router.py`'s old `# SECURITY GAP` comment said exactly this).

`get_current_user` is the piece that turns "a request carrying some `Authorization` header" into "an actual, verified `User` row" — the thing every route that needs to know "who's calling" can depend on, instead of trusting whatever the client claims about its own identity.

## 2. `HTTPBearer`: extracting the token, and nothing more

```python
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer_scheme = HTTPBearer()
```

`HTTPBearer` is FastAPI's built-in tool for one narrow job: read the `Authorization` header, confirm it's shaped like `Bearer <token>`, and hand back an `HTTPAuthorizationCredentials` object whose `.credentials` attribute is just the token string. It does **not** decode, verify, or know anything about JWTs specifically — it only knows "HTTP's standard Bearer-auth header format." If the header is missing or malformed entirely, FastAPI rejects the request with a `403` automatically, before `get_current_user`'s own body ever runs.

**Why `HTTPBearer`, not `OAuth2PasswordBearer`** (FastAPI's other common option for this): `OAuth2PasswordBearer` assumes a full OAuth2 password-grant contract — it wants a `tokenUrl` pointing at a login endpoint that accepts form-encoded `username`/`password` fields. Our actual `/auth/login` (BOOK-30) takes a JSON body (`UserLogin`), not that shape. Using `OAuth2PasswordBearer` would advertise a login flow we don't actually have; `HTTPBearer`'s plainer "just extract the header" behavior matches what was really built.

`bearer_scheme = HTTPBearer()` is created once at module level, not inside the function — same reasoning as `get_settings()` being `@lru_cache`'d (`core/README.md`): no need to reconstruct this object on every single request.

## 3. The dependency chain: `Depends` composing `Depends`

```python
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    uow: UnitOfWork = Depends(get_uow),
) -> User:
```

`get_current_user` is itself a FastAPI dependency (used elsewhere as `Depends(get_current_user)`), but it *also* depends on two other things: `bearer_scheme` (to get the raw token) and `get_uow` (to look the user up in the database, section 5). FastAPI resolves this whole chain automatically — a route that writes `current_user: User = Depends(get_current_user)` doesn't need to separately declare `Depends(bearer_scheme)` or `Depends(get_uow)` itself; FastAPI walks the dependency graph and satisfies all of it. This is the same `Depends`-based dependency injection covered generally in `docs/db/dependency-injection-basics.md`, just chained one level deeper than `get_uow` alone.

## 4. One generic `401` for every failure — same pattern as login and refresh

```python
unauthorized = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)
```

Built once, then raised from three different, unrelated failure points below. This is the exact same "don't leak which specific check failed" reasoning as `user-enumeration-basics.md` (login) and `/auth/refresh` (BOOK-31): a caller learns only "this didn't work," never *why* — not "the token expired" vs. "you used a refresh token here" vs. "that user was deleted." Distinguishing those would hand an attacker extra information for free, with no legitimate use to a real client (a real client doesn't need to know *which* reason its stale token failed — it just needs to log in again either way).

The `headers={"WWW-Authenticate": "Bearer"}` part is standard HTTP: [RFC 7235](https://datatracker.ietf.org/doc/html/rfc7235#section-3.1) says a `401` response should include a `WWW-Authenticate` header telling the client what kind of auth scheme is expected — here, `Bearer`, matching section 2.

## 5. The three checks

```python
try:
    payload = decode_token(credentials.credentials)
except TokenError as exc:
    raise unauthorized from exc

if payload.get("type") != "access":
    raise unauthorized

user = await uow.users.get_by_id(uuid.UUID(payload["sub"]))
if user is None:
    raise unauthorized

return user
```

1. **Decode.** `decode_token` (`security.py`, `jwt-basics.md` §8) verifies the signature and checks `exp`. Anything wrong here — tampered, expired, malformed, wrong secret — raises `TokenError`, caught and turned into the generic `401`.
2. **Type check.** Rejects a *refresh* token presented here — the exact mirror image of `refresh_tokens()` rejecting an *access* token (BOOK-31, `jwt-basics.md` §7). Without this, a client could use its long-lived refresh token directly as if it were an access token on every protected route, defeating the entire short-lived-access design (`jwt-basics.md` §5).
3. **User still exists.** `payload["sub"]` only proves *a* user id was validly signed by us at some point in the past — not that the account is still around *now*. A deleted account holding an otherwise well-formed, unexpired access token should not still resolve to a real `User`. `uow.users.get_by_id` (inherited from `BaseRepository`) is the check.

Only if all three pass does the function return an actual `User` object — the thing a route can now trust, instead of trusting whatever the client claimed in a query parameter.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `HTTPBearer` | Extracts the raw token from `Authorization: Bearer <token>` — does not verify anything itself |
| `OAuth2PasswordBearer` (not used here) | FastAPI's other option; assumes a form-encoded OAuth2 login contract we don't have |
| Dependency chain | `get_current_user` depends on `bearer_scheme` and `get_uow`; FastAPI resolves the whole graph automatically from one `Depends(get_current_user)` in a route |
| Generic 401 | One error for decode failure, wrong token type, and deleted user — same "don't leak which check failed" reasoning as login/refresh |
| Type check | Rejects a refresh token used as an access token — the inverse of `refresh_tokens()`'s own check |
