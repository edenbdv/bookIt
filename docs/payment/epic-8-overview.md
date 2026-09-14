# Epic 8 overview — briefing doc for the NotebookLM explainer

This file isn't a source of new information — it's a guide for what to emphasize
when generating an explainer from the other Epic 8 docs. Attach it alongside
`saga-basics.md`, `webhook-basics.md`, and `payment-decisions.md`.

## What this epic is, in one sentence

How BookIt safely charges a guest and keeps a reservation's state in sync with
that charge — including every way the charge itself can go wrong.

## The story arc — explain it in this order

1. **The core problem.** Booking a reservation and charging for it are two
   separate operations against two different concerns (a reservation's state
   machine, an external payment call) that must end up *consistent* — a
   confirmed reservation with no successful charge, or a charge with no
   confirmed reservation, are both wrong. No database transaction can span an
   external HTTP call, so this can't be solved with a single commit.

2. **The saga pattern is the answer** — `saga-basics.md`. One function
   explicitly sequences "charge, then react to the outcome" (orchestration,
   not choreography): success confirms the reservation, failure cancels it,
   and a successful charge that somehow can't confirm gets *compensated*
   (refunded, then released) rather than left inconsistent.

3. **The three-outcome shape.** A real charge attempt doesn't just
   succeed or fail — it can also time out with no answer at all. Emphasize
   that `TIMEOUT` is deliberately not the same as `FAILURE`: a failure means
   the provider said no; a timeout means *unknown*, because the charge may
   have actually gone through with the response lost in transit. This
   distinction is what the rest of the epic is built around.

4. **Idempotency makes retries safe.** A guest's request can be retried
   (network hiccup, a double click) without risking a double charge, because
   of idempotency keys — a retry with the same key returns the original
   result instead of charging again.

5. **The commit-split.** A payment row is committed as `PENDING` *before*
   the external charge call, not after — so a crash mid-call still leaves a
   durable, resolvable record instead of losing the attempt entirely.

6. **Resolving a `TIMEOUT` — two ways to the same answer.** This is the
   crux of the epic, worth the most explanation time:
   - **Push**: `webhook-basics.md` — the provider calls back later with the
     real outcome, verified with an HMAC signature over the raw request body.
   - **Pull**: a reconciliation worker periodically asks the provider directly
     for anything left `PENDING` too long, in case the push never arrives.
   - Both paths resolve through the *exact same function* — the webhook and
     the reconciliation worker are two triggers for one resolution, not two
     separate implementations.

7. **Guarding the waiting period.** While a payment is genuinely unresolved,
   a guest retrying shouldn't be able to start a second, independent charge
   attempt — closing that gap is what makes steps 3–6 actually safe in
   practice, not just theoretically correct.

8. **Cancellation refunds.** A guest cancelling gets a policy-based partial
   refund; a host cancelling on a guest gets a full refund regardless of
   policy — same underlying refund machinery, different amount rule for a
   deliberately different reason (a guest shouldn't be penalized for a host's
   decision).

## Where each part of the story actually lives

| Story step | Read this |
|---|---|
| 1. The core problem | `saga-basics.md` §0 (mental model) and §1 ("Why 'just use a transaction' doesn't apply here") |
| 2. The saga pattern | `saga-basics.md` §2–4 (the core idea, when a saga is actually needed, orchestration vs. choreography) + `payment-decisions.md` §5 ("BOOK-54: why this needs a saga at all") |
| 3. The three-outcome shape, TIMEOUT vs FAILURE | `payment-decisions.md` §1 ("BOOK-52: the mock provider's outcome...") |
| 4. Idempotency | `saga-basics.md` §5 ("Idempotency — a saga's own steps must tolerate being re-entered") + `payment-decisions.md` §2 and §4 |
| 5. The commit-split | `saga-basics.md` §6 ("The gap no amount of local commit ordering closes") + `payment-decisions.md` §6 ("Split the commit") |
| 6. Push resolution (webhook) | `webhook-basics.md` §1–4 (why async can't answer inline, correlation id, webhook vs. polling, the gap it closes) and §8 (proving who's calling) + `payment-decisions.md` §8 |
| 6. Pull resolution (reconciliation) | `payment-decisions.md` §11 ("BOOK-101: a reconciliation worker") |
| 7. Guarding the waiting period | `payment-decisions.md` §10 ("Two small, related fixes found by asking 'why does paying feel instant?'") |
| 8. Cancellation refunds | `payment-decisions.md` §9 ("BOOK-76: cancellation policies driving refund amounts") |

Skip `saga-basics.md`/`webhook-basics.md`'s own "Cheat sheet" sections and
`payment-decisions.md` §3, §7 — real, but secondary detail (idempotency-key
cleanup scheduling; compensating a successful-but-unconfirmable charge) that
isn't part of the core arc above.

## What to de-emphasize

Skip deep detail on the frontend React state machines, the specific HTTP
status codes, and BookIt-specific ticket numbers (BOOK-52, BOOK-55, etc.) —
those are implementation bookkeeping, not part of the pattern being taught.
The saga pattern, the push/pull resolution split, and the TIMEOUT-vs-FAILURE
distinction are the three ideas worth a viewer actually retaining.
