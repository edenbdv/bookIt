"""Server-side refresh-token state: rotation and revocation (BOOK-78).

The only stateful piece of auth. Everything else (`security.py`, `dependencies.py`)
decides validity from the signature and `exp` alone, which is why a refresh token
used to be replayable until it expired -- nothing recorded that it had been spent.

One key per session holds the single refresh `jti` currently valid for it, plus a
short-lived key holding the one just spent (the grace window -- see `rotate`).
That's an allowlist (what's alive), not a blocklist (what's dead), and the
difference matters: killing a session means deleting one key, which works even
for a token we're not being shown -- one in a thief's hands, or on a lost phone.

Only ids are ever stored, never token strings: a `jti` is inert without
`secret_key`, so a Redis leak hands over nothing usable. Full reasoning in
docs/auth/refresh-token-rotation-basics.md.

Redis errors are deliberately NOT caught here -- same contract as
core/rate_limit.py's primitives. Refresh IS the security control, so the caller
fails closed (503) rather than falling back to honouring any signed token.
"""

from app.core.config import get_settings
from app.core.redis_client import get_redis_client


def _session_key(session_id: str) -> str:
    return f"refresh:session:{session_id}"


def _prev_key(session_id: str) -> str:
    # the jti this session just spent, kept only for refresh_reuse_grace_seconds
    return f"refresh:prev:{session_id}"


def _user_sessions_key(user_id: str) -> str:
    return f"refresh:user:{user_id}"


def _session_ttl_seconds() -> int:
    return get_settings().refresh_token_expire_days * 24 * 60 * 60


def _text(value: bytes | str | None) -> str | None:
    return value.decode() if isinstance(value, bytes) else value


async def start_session(user_id: str, session_id: str, jti: str) -> None:
    """Record a brand-new session's first refresh jti (login)."""
    redis = get_redis_client()
    ttl = _session_ttl_seconds()

    pipe = redis.pipeline()
    pipe.set(_session_key(session_id), jti, ex=ttl)
    # index of a user's live sessions, so "log out everywhere" doesn't need a
    # scan over the keyspace. Its TTL is pushed forward on every new login --
    # the set outliving a single session is harmless, entries are only ever
    # read to be deleted
    pipe.sadd(_user_sessions_key(user_id), session_id)
    pipe.expire(_user_sessions_key(user_id), ttl)
    await pipe.execute()


async def rotate(session_id: str, presented_jti: str, new_jti: str) -> str | None:
    """Spend `presented_jti`. Returns the jti the caller should mint with, or None to reject.

    Three cases:

    - It's the session's current token -> rotate. `new_jti` becomes current, and
      the spent one is remembered briefly so the grace case below can recognise
      it. Returns `new_jti`.
    - It's the token we just spent, still inside the grace window -> two clients
      refreshed at nearly the same moment (two browser tabs, typically). Not
      theft. Returns the *current* jti rather than rotating again, so both
      callers converge on one token instead of forking the chain.
    - Anything else -> it was signed by us and hasn't expired, so it's a copy of
      a token spent long enough ago that no honest client would still be holding
      it. Treated as theft: the session is deleted, which kills the token the
      thief holds too, not just the one they showed us. Returns None, as does a
      session that's simply gone (logged out, revoked, expired) -- the caller's
      401 is identical either way.

    Not atomic: two refreshes landing inside the same Redis round trip can both
    read the same current jti and both rotate. That needs a Lua compare-and-set,
    which was judged not worth it -- the realistic gap between two tabs is
    milliseconds, not microseconds, and the grace window already covers that.
    """
    settings = get_settings()
    redis = get_redis_client()

    stored = _text(await redis.get(_session_key(session_id)))
    if stored is None:
        return None

    if stored == presented_jti:
        pipe = redis.pipeline()
        pipe.set(_session_key(session_id), new_jti, ex=_session_ttl_seconds())
        pipe.set(_prev_key(session_id), presented_jti, ex=settings.refresh_reuse_grace_seconds)
        await pipe.execute()
        return new_jti

    if _text(await redis.get(_prev_key(session_id))) == presented_jti:
        return stored

    pipe = redis.pipeline()
    pipe.delete(_session_key(session_id))
    pipe.delete(_prev_key(session_id))
    await pipe.execute()
    return None


async def revoke_session(user_id: str, session_id: str) -> None:
    """Log out one session. Its refresh chain dies immediately."""
    redis = get_redis_client()
    pipe = redis.pipeline()
    pipe.delete(_session_key(session_id))
    pipe.delete(_prev_key(session_id))
    pipe.srem(_user_sessions_key(user_id), session_id)
    await pipe.execute()


async def revoke_all_sessions(user_id: str) -> int:
    """Log out every device. Returns how many sessions were killed."""
    redis = get_redis_client()
    key = _user_sessions_key(user_id)
    session_ids = await redis.smembers(key)
    if not session_ids:
        return 0

    pipe = redis.pipeline()
    for raw_sid in session_ids:
        sid = _text(raw_sid)
        pipe.delete(_session_key(sid))
        pipe.delete(_prev_key(sid))
    pipe.delete(key)
    await pipe.execute()
    return len(session_ids)
