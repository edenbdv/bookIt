"""Manual verification of BOOK-78's refresh-token rotation and revocation,
through the running HTTP API.

Not a pytest test -- same standing reason as verify_concurrent_reservations.py
(BOOK-39) and verify_no_overlapping_reservations.py (BOOK-24): there's no real
suite yet, see BOOK-61/BOOK-62. Rotation earns a script of its own because its
whole point is a *sequence* of calls -- one token working exactly once, and the
same token failing the second time -- which no single request can demonstrate.

Case 2 is the one worth reading: replaying a just-spent token inside the grace
window must SUCCEED. That's two browser tabs refreshing within milliseconds of
each other -- each has its own interceptor and neither knows the other exists,
which no amount of client-side single-flight fixes. Without the grace window
that is indistinguishable from theft, and a real user gets logged out.

Known limitation, deliberately not tested (it isn't deterministic): two
refreshes landing inside the same Redis round trip can both rotate. That needs
a Lua compare-and-set; the realistic tab-to-tab gap is milliseconds, which
case 2 covers.

Requires: `docker compose up -d app gateway` (the API is reached through the gateway, :8080).
Run with: uv run python scripts/verify_refresh_token_rotation.py
"""

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

API_BASE_URL = "http://localhost:8080"  # BOOK-67: the gateway
PASSWORD = "verify-password-123"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' -- {detail}' if detail else ''}")
    if not ok:
        _failures.append(label)


async def signup_and_login(client: httpx.AsyncClient) -> dict:
    email = f"rotation_{uuid.uuid4().hex[:12]}@example.com"
    await client.post(
        "/auth/signup",
        json={"email": email, "display_name": "Rotation Verify", "password": PASSWORD},
    )
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    resp.raise_for_status()
    return resp.json()


async def main() -> None:
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=10.0) as client:
        print("\n1. Rotation: the current refresh token is accepted exactly once")
        tokens = await signup_and_login(client)
        first = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        check("first refresh succeeds", first.status_code == 200, f"got {first.status_code}")
        rotated = first.json()
        check(
            "a genuinely different refresh token comes back",
            rotated["refresh_token"] != tokens["refresh_token"],
        )

        print("\n2. Grace window: the two-tabs race is NOT treated as theft")
        # same token again, immediately -- what a second browser tab does
        raced = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        check(
            "replaying within the grace window succeeds",
            raced.status_code == 200,
            f"got {raced.status_code}",
        )
        survivor = raced.json()["refresh_token"]
        still_live = await client.post("/auth/refresh", json={"refresh_token": survivor})
        check(
            "and the session is still usable afterwards",
            still_live.status_code == 200,
            f"got {still_live.status_code}",
        )

        print("\n3. Reuse detection: the same token past the grace window IS theft")
        tokens = await signup_and_login(client)
        first = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        rotated = first.json()
        await asyncio.sleep(11)  # refresh_reuse_grace_seconds default is 10
        replay = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        check(
            "replaying the spent token is 401",
            replay.status_code == 401,
            f"got {replay.status_code}",
        )

        print("\n4. Theft response: detecting reuse kills the whole session, not just that token")
        # the rotated token was still perfectly valid before case 3 -- it must
        # now be dead too, or a thief's replay would cost the attacker nothing
        after = await client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
        check(
            "the still-current token is dead too",
            after.status_code == 401,
            f"got {after.status_code}",
        )

        print("\n5. Revocation: logout ends the session immediately")
        tokens = await signup_and_login(client)
        out = await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        check("logout returns 204", out.status_code == 204, f"got {out.status_code}")
        post_logout = await client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        check(
            "refreshing after logout is 401",
            post_logout.status_code == 401,
            f"got {post_logout.status_code}",
        )
        again = await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        check("logout is idempotent", again.status_code == 204, f"got {again.status_code}")

        print("\n6. A rotated token keeps working across several refreshes in sequence")
        tokens = await signup_and_login(client)
        current = tokens["refresh_token"]
        chain_ok = True
        for _ in range(3):
            resp = await client.post("/auth/refresh", json={"refresh_token": current})
            if resp.status_code != 200:
                chain_ok = False
                break
            current = resp.json()["refresh_token"]
        check("three sequential refreshes all succeed", chain_ok)

        print("\n7. logout-all ends every session, and only this user's")
        a = await signup_and_login(client)
        email_b = f"rotation_{uuid.uuid4().hex[:12]}@example.com"
        await client.post(
            "/auth/signup",
            json={"email": email_b, "display_name": "Other User", "password": PASSWORD},
        )
        other = (
            await client.post("/auth/login", json={"email": email_b, "password": PASSWORD})
        ).json()

        killed = await client.post(
            "/auth/logout-all", headers={"Authorization": f"Bearer {a['access_token']}"}
        )
        check("logout-all returns 204", killed.status_code == 204, f"got {killed.status_code}")
        mine = await client.post("/auth/refresh", json={"refresh_token": a["refresh_token"]})
        check("my session is gone", mine.status_code == 401, f"got {mine.status_code}")
        theirs = await client.post("/auth/refresh", json={"refresh_token": other["refresh_token"]})
        check(
            "another user's session is untouched",
            theirs.status_code == 200,
            f"got {theirs.status_code}",
        )

    print("\n" + ("FAILED: " + ", ".join(_failures) if _failures else "All checks passed."))
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
