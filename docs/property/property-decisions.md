# What BookIt chose for the property/host side, and why

Same shape as `docs/payment/payment-decisions.md`: what was picked, what was rejected,
and when to revisit.

## 1. BOOK-102: a minimal host UI, and the backend gaps it exposed

**The starting problem:** the frontend had zero host-facing surface — `Feed`,
`PropertyDetail`, `Login`, `Signup` are all guest browsing/booking. A host account had
nowhere to see or manage anything as a host, even though most of the backend already
existed (`POST/PATCH/DELETE /properties/{id}`, photo endpoints, and
`POST /reservations/{id}/reviews` already inferring the author as guest *or* host).
What was actually missing wasn't UI alone — there was no way to even *query* "my
properties" or "reservations on my property" at all.

### 1.1 `host_id` as a plain filter on the existing public listing endpoint

**Chosen:** `GET /properties?host_id={id}` — one more optional filter on
`list_filtered()`, same as `city`/`guests`/price range. No new endpoint, no auth
required: which properties a host owns is public information already visible by
browsing, same as filtering by city.

### 1.2 A property's reservations are a reservation-module endpoint, not a property-module one — even though the URL says `/properties/...`

**Chosen:** `GET /properties/{property_id}/reservations` lives in
`reservation/router.py`, which gave up its `/reservations` prefix and now writes each
route's full path explicitly. This mirrors `review/router.py`'s existing, deliberate
choice (its own comment: *"URL nesting doesn't decide module ownership, return type
does"*) — that file already owns endpoints under both `/reservations/...` and
`/properties/...` for the same reason. Extended an existing precedent rather than
inventing a second convention.

**What this doesn't change:** which module owns which table's invariants. Reservation
still exclusively owns `Reservation`'s state machine; property still exclusively owns
`Property`. `reservation/service.py` reaching into `uow.properties.get_active_by_id()`
isn't new coupling either — `create_reservation()` already did that, to check
self-booking and `guest_count` against `max_guests`.

**Auth shape:** host-only, not public — unlike the `host_id` filter above, this
returns guest identities and booking details. `current_user` (token) says *who's
asking*; `property_id` (URL) says *which property's bookings*. A host can own more than
one property, so the token alone can't answer "which one" — both are needed, for
different jobs.

### 1.3 `GET /auth/me` — the missing "who am I", instead of decoding the JWT client-side

**The gap:** the frontend's `Session` never carried the logged-in user's own id —
needed to actually call the `host_id` filter above. The access token's `sub` claim
technically already has it.

**Chosen:** a real endpoint (`UserRead`, reusing the existing `get_current_user`
dependency) over decoding the token client-side. An access token's job is
authorization, not identity delivery — the same reason OAuth splits `access_token` from
`id_token` rather than overloading one. Decoding it in the frontend would also only
ever yield the bare id (`sub`)/session id (`sid`); anything else (`display_name`, etc.)
would still need a real fetch. And it isn't scoped to this one ticket — nothing in the
frontend could answer "who's logged in" before this, so the next feature that needs it
would have hit the same wall.

### 1.4 Deleting a property doesn't check for open reservations — until this ticket

**The bug, found by asking "what if a host deletes a property with a future booking?":**
`delete_property()` only ever set `deleted_at`, with no check against `Reservation` at
all. A guest with a `CONFIRMED`, paid, future stay kept a completely untouched
reservation — but the property now 404s (`get_active_by_id` excludes delisted rows) and
disappears from search. The reservation itself stayed valid (cancel/refund still worked,
since `cancellation.py` looks the property up via the unfiltered `get_by_id`), but
became invisible: there's still no "my bookings" list for guests (BOOK-85/BOOK-98), so
there was no page anywhere that would show it to them.

**Chosen:** `ReservationRepository.has_active_by_property_id()` — `PENDING` or
`CONFIRMED` (the two non-terminal statuses, same set `_LEGAL_TRANSITIONS` already
encodes) blocks the delete with a `409`. `CANCELLED`/`COMPLETED` don't block it — nothing
left to disrupt.

**Ordering:** ownership (`403`) is still checked before this (`409`) — same "existence
and ownership outrank everything else" order `update_property`/`delete_property` already
used; a non-owner has no business learning whether the property has active bookings.

### 1.5 Property photos stay URL-only; no upload was built

**Chosen:** the add-photo form takes a pasted URL (`PropertyPhotoCreate.url: str`),
same contract the seed data and `PhotoCarousel` already assumed. A failed image load
now shows a visible "Couldn't load" placeholder with the offending URL instead of
silently disappearing — cheap, and it turns a silent failure into a diagnosable one.

**Why real upload wasn't built:** the honest version isn't "add a file input" — it's
direct-to-storage upload via pre-signed URLs (client uploads straight to S3, bypassing
the app server for the bytes), a CDN in front of storage, and an async thumbnailing/
EXIF-stripping/content-sniffing pipeline. That's meaningfully more infrastructure than
this ticket's scope, and most of it (CDN, malware scanning) is infrastructure a real
team buys rather than builds. Local-filesystem storage was considered and rejected for
now too: it would work today (the backend runs directly via `uvicorn`, not
containerized, so disk persistence isn't the blocker), but it's a dead end that doesn't
resemble the real pattern, so it wasn't worth building either.

**Revisit when:** if this ever becomes a learning exercise in its own right, the
pre-signed-URL flow (1.5's "chosen" paragraph) is the one piece worth actually building;
the rest is reasonably left as "this is what real apps do," not re-implemented.

### 1.6 What this unblocks

BOOK-80 (host-initiated cancellation) was deprioritized earlier specifically because
there was no host UI to trigger it from — that blocker is gone now. BOOK-98/BOOK-85
(a live-updating payment UI, and `GET /reservations/{id}`) remain untouched; this
ticket's `GET /properties/{id}/reservations` answers "reservations for one property",
not "one reservation by id" or "my reservations as a guest" — a different, still-open
gap.
