# What BookIt chose for its microservice split, and why

The Epic 11 counterpart to `microservices-basics.md`: that page explains the general
concepts and the overall plan; this one records what this project actually decided,
ticket by ticket, and when each decision should be revisited. Same format as
`docs/jobs/worker-decisions.md` and `docs/testing/testing-decisions.md`.

---

# BOOK-64 — Notification as its own service

## 1. The starting point: six ties to the monolith

Notification already consumes Kafka in its own process (BOOK-51), but it's still a
module of the monolith wearing a separate container. Six things tie it to Booking:

| # | Coupling | Where |
|---|---|---|
| 1 | All three email functions read Booking's tables (reservation, guest, property) | `notification/service.py` — `uow.reservations` / `uow.users` / `uow.properties` |
| 2 | The event is too thin to send an email from: just `event_id`, `event_type`, `reservation_id`, `occurred_at` | `reservation/service.py`, `_emit_outbox_event` |
| 3 | A foreign key *between services*: the Inbox table points at Booking's outbox table | `notification/models.py` — `ForeignKey("outbox_events.id")` |
| 4 | Shared code: the monolith's `UnitOfWork`, `get_engine`, `get_settings`, `OutboxEventType` | `notification/consumer.py`, `app/notification_consumer.py` |
| 5 | The "reservation received" email runs in the monolith's Celery worker, triggered by `.delay()` | `app/worker.py`, `reservation/service.py` |
| 6 | Same image (`build: .`) and the same `bookit` database | `docker-compose.yml`, `notification-consumer` service |

## 2. The plan: six small steps, the app works after each

1. **Lock the current behavior with tests** — the Inbox dedup and the email contents —
   before changing anything (BOOK-60 item 3).
2. **Enrich the event** with everything the emails need, and switch the handlers to
   read the payload instead of the DB. Still inside the monolith. Removes ties 1–2.
3. **Move "reservation received" to Kafka** as `BookingCreated` (§4). Removes tie 5.
4. **Own database + migrations** for Notification; drop the cross-service foreign key.
   Removes tie 3.
5. **Extract the code** into its own service with no imports from `app`, its own image
   and compose service. Removes ties 4 and 6.
6. **Tests + this doc** brought up to date.

## 3. Repo structure: a separate service folder in the same repo (monorepo)

**Chosen:** `services/notification/` — its own `pyproject.toml`, `Dockerfile`, config
and migrations, in the same Git repo as the monolith.

| Option | Why not / why |
|---|---|
| **A. `services/notification/` in this repo** ✅ | Real independence: own dependencies, own image. It physically *can't* import from `app` — the structure itself enforces the boundary. Still one repo, so one PR and one CI run for a change that touches both sides of an event |
| B. Stay inside `app/`, promise not to import | Nothing stops an `import app.reservation...` slipping in by accident — the classic start of a distributed monolith (`microservices-basics.md` §4) |
| C. A separate Git repo | Full separation, but every event change becomes two PRs and two CI runs. Too much overhead for one developer |

**Bonus:** BOOK-65 then lands as `services/payment/` right next to it — same layout,
same CI pattern.

**Revisit when:** different people own different services and want separate release
cycles — then a split into repos (option C) starts paying for itself.

## 4. "Reservation received" moves from Celery to Kafka (`BookingCreated`)

**Chosen:** Booking publishes a `BookingCreated` event through its existing outbox;
Notification consumes it from `booking-events` like the other two. All three emails
arrive the same way.

**This revisits an explicit earlier decision.** `worker-decisions.md` §9 kept this email
on a direct Celery `.delay()` "permanently": one action → one email is the textbook
task-queue case, and routing it through Kafka would be forcing every side effect
through an event bus just because one exists. **That reasoning was right for a
monolith.** §9's own "revisit when" was *"a second real consumer of 'reservation
created' gets planned"* — and once Notification is a separate service, the event bus
*is* the interface between services. The circumstances changed, so the decision does.

### Why a Celery task across services is coupling — command vs. event

Both are contracts, so coupling never drops to zero. What differs is **who depends on
whom**, and **how much the sender knows about the receiver**.

**A Celery task is a command** — "run this function for me":

```python
send_reservation_received_email.delay(reservation_id)
```

To send it, Booking must know that Notification exists, that it's responsible for this
email, the task's exact name, and its arguments. **Booking depends on Notification**:
rename the task, add an argument, split it in two, and Booking's code must change — or
it fails silently, with a message on RabbitMQ that no worker recognizes.

**A Kafka event is a fact** — "this happened":

```
BookingCreated { event_id, reservation_id, guest_email, property_title, check_in, ... }
```

Booking says what happened and nothing more. It doesn't know who's listening or that an
email exists. The rule "a new booking means an email" lives inside Notification.
**Notification depends on Booking** (on the event's shape) — the natural direction:
whoever reacts depends on whoever reports.

| Scenario | Celery (command) | Kafka (event) |
|---|---|---|
| Notification renames its function | Booking breaks | Booking unaffected |
| Also send an SMS | Booking adds another call | Notification decides alone |
| An analytics service wants new bookings | Booking adds another call | It subscribes; Booking unchanged |
| Notification is down | Task waits in RabbitMQ | Event waits in Kafka |

**The honest caveat:** with an event, the *payload's shape* becomes the contract —
remove `guest_email` and Notification breaks. That's still coupling, but on data rather
than another service's internals, and pointing the right way.

### Further reasons, specific to BookIt

- **One inbound channel.** Notification listens only to Kafka — no Celery worker and no
  RabbitMQ dependency inside it.
- **Durability.** The current trigger is `uow.on_commit()`, which `worker-decisions.md`
  §9 itself notes is in-memory only: a crash right after commit silently loses the
  email. The outbox writes the event in the same transaction as the reservation.
- **The blocking `.delay()` goes away on this path.** BOOK-62 measured Celery's `.delay()`
  freezing the event loop for ~4s per booking when RabbitMQ is unreachable. An outbox
  row is a plain insert.

**Cost accepted:** one more outbox row per new booking, and an ordering point to
respect — `BookingCreated` goes on the same topic, keyed by `reservation_id`, so it
always precedes that reservation's `BookingConfirmed` / `BookingCancelled`
(`kafka-basics.md` §3).

**Revisit when:** never in the "back to Celery" direction while Notification is a
separate service.

## 5. Events carry the data (event-carried state transfer)

**Chosen:** the event payload includes everything an email needs — guest email and name,
property title / city / country, dates, guest count, total, hold expiry — so
Notification never reads Booking's data (`microservices-basics.md` §3.4).

**Not chosen:** Notification calling Booking's API for the details. It works, but puts
a synchronous dependency back: Booking down ⇒ no emails, even though the event itself
arrived fine.

**Cost accepted:** bigger events, and data copied at event time. That copy is a feature
here, not a bug: an email should describe the booking *as it was* when the event
happened, not whatever the reservation looks like by the time the email is sent.

**The payload is now a contract** between two services: add fields freely; never
remove or rename one without updating the consumer first.

**Revisit when:** a payload grows large or carries data Booking shouldn't share — then
send only what each consumer needs, or fall back to a lookup for that one field.

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| BOOK-64 repo structure | `services/notification/` in the same repo (monorepo) | Separate owners want separate release cycles |
| "Reservation received" trigger | Kafka `BookingCreated` via the outbox — revisits `worker-decisions.md` §9 | Never back to Celery while Notification is separate |
| Why not a Celery task across services | A command makes the sender know the receiver; an event lets the receiver know the sender | — |
| Email data | Carried in the event payload, no lookups into Booking | Payload grows too large or too sensitive |
| Event payload | A cross-service contract: add freely, never remove/rename first | — |
