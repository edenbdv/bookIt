# Microservices: the concepts that matter, and where BookIt answers them

A study and interview map for Epic 11. For each concept: the question people actually
ask, BookIt's short answer, and where the full reasoning lives. The theory is in
`microservices-basics.md`; the decisions in `microservices-decisions.md` (§ numbers
below refer to it unless another file is named).

The real test for every row: can you say **why** BookIt chose it, **what it costs**,
and **when you'd choose differently**?

| # | Concept | The question | Status |
|---|---|---|---|
| 1 | Why split, and where | "When would you *not* split?" | ✅ |
| 2 | Sync vs async | "REST or events — when which?" | ✅ |
| 3 | Outbox + Inbox | "How do you not lose or duplicate events?" | ✅ |
| 4 | Saga + compensation | "No distributed transaction — what if step 2 fails?" | ✅ |
| 5 | Eventual consistency in the UI | "What does the user see in between?" | ✅ |
| 6 | Database per service | "How do you join across services? Foreign keys?" | ✅ |
| 7 | Failure handling | "Service down, slow, poison message?" | ✅ / ⏳ BOOK-68 |
| 8 | Service-to-service auth | "How does a service prove who it is?" | ✅ |
| 9 | API gateway | "How does the frontend reach many services?" | ⏳ BOOK-67 |
| 10 | Observability / tracing | "How do you follow one payment across services?" | ❌ not planned |

## 1. Why split, and where to draw the boundary

**Answer:** only two services leave the monolith — Notification (fault isolation, only
receives) and Payment (an external system, security, its own data). Users, properties,
reservations and reviews stay together: they work together on almost every request, so
splitting them would turn JOINs into chatty network calls.
**Cost of splitting:** network calls, eventual consistency, more to run and debug.
**Where:** §0.

## 2. Sync vs async communication

**Answer:** *react* to something that happened → an event (Notification's emails,
Payment's refunds). *Decide* something now while someone waits → a sync call (Payment
reads the reservation before charging). Exactly one sync call between the services.
**Cost of sync:** a runtime dependency — Booking down means nobody can pay.
**Where:** §17; the flow in `epic-11-flow.md`.

## 3. Outbox + Inbox: at-least-once, without duplicates

**Answer:** the event row is written in the **same transaction** as the change it
describes (outbox), so it can't be lost; a relay publishes it. Delivery is at-least-once,
so every consumer dedups on `event_id` (Inbox) — in the same transaction as its effect
where possible (Booking's confirm, Payment's PENDING refund row).
**War story:** the relay marked messages "published" when they hadn't *failed yet* — a
missing topic silently lost an event. Fix: mark only Kafka-*confirmed* ones.
**Where:** §12–§15; `docs/jobs/outbox-basics.md`, `inbox-basics.md`.

## 4. Saga and compensation

**Answer:** no two-phase commit. Charge → `payment_succeeded` → Booking confirms; on
failure, a **compensating** action: a payment for an already-cancelled reservation comes
back as `booking_payment_rejected` and Payment refunds it. Choreography (events), not
one orchestrator function.
**Cost:** the flow is spread across services — harder to see end to end (→ #10).
**Where:** §15; `docs/payment/saga-basics.md`.

## 5. Eventual consistency in the UI

**Answer:** "payment succeeded" no longer means "reservation confirmed" — that follows a
few seconds later. The frontend polls the reservation instead of guessing; the refund
amount on cancel is shown at once because Booking computes it in the request.
**Where:** §16.

## 6. Database per service

**Answer:** each service owns its tables; nobody reads another's. No foreign keys across
services — a plain id instead. Data another service needs travels in events (Notification)
or via an API (Payment's sync call).
**Status:** both have their own — Notification (BOOK-64 §8) and Payment (BOOK-65 §18:
new DB, history copied, old tables dropped by a contract migration).
**Where:** §8, §17.

## 7. Failure handling

**Answer:** a crashed consumer restarts and Kafka redelivers the uncommitted message; a
message that fails for good goes to a dead-letter topic so the queue keeps moving; the
sync call has a 2 s timeout → 503 "try again", with nothing charged.
**Known gap:** a confirm that *crashes* goes to the DLQ — nobody refunds yet (BOOK-68).
**Where:** §7, §13, §17.

## 8. Service-to-service auth

**Answer:** a short-lived service token (JWT, `type: "service"`) signed with its own
secret; a guest token doesn't work on `/internal`, a service token doesn't work on guest
routes. Considered mTLS (stronger, more infra) and RS256 (no shared secret).
**Where:** `docs/auth/service-token-basics.md`.

## 9. API gateway

**Answer (planned):** one public entry point — routes `/payments/*` to Payment and the
rest to Booking, keeps `/internal/*` closed, one place for CORS, likely verifies the
guest's JWT so Payment never needs the user-login secret.
**Where:** `microservices-basics.md` §3.9; BOOK-67.

## 10. Observability: following one request across services

**Answer (not built):** a `correlation_id` created at the first request and carried in
every event payload and every log line, so one payment can be traced through Payment,
Kafka and Booking. Full version: distributed tracing (OpenTelemetry).
**Why it matters:** with choreography (#4), there's no single place that shows the flow.
**Status:** not in Epic 11 — worth a small ticket.
