# Service token basics (BOOK-66)

**Status: design — not built yet.** For the general theory see
`docs/microservices/microservices-basics.md` §3.8; for why Payment calls Booking
synchronously at all, `docs/microservices/microservices-decisions.md` §17. JWT itself is
explained in `jwt-basics.md`.

## 1. The problem: a call with no user behind it

Inside the monolith, Payment reads a reservation with a function call — no auth needed,
it's the same program. Once Payment is its own service, it asks Booking over HTTP:
`GET /internal/reservations/{id}`. That endpoint returns a reservation's guest and
amount, so it can't be open to whoever reaches Booking on the network.

But there's **no user** to check here. The caller is Payment, a program. So it needs its
own kind of proof: a **service token** — a JWT that says "I am the Payment service".

## 2. Two tokens, two different calls

```
Guest's browser ──(user token)──► Payment: POST /payments
                                     │
                                     └──(service token)──► Booking: GET /internal/reservations/{id}
```

| Call | Who calls | Token | What it proves |
|---|---|---|---|
| `POST /payments` | The **guest** (browser) | The **user** token from login — unchanged | *Who* is paying → `guest_id` |
| `GET /internal/reservations/{id}` | **Payment** (a service) | The **service** token | The caller *is* Payment |

The guest proves who they are to Payment; Payment proves it's Payment to Booking. Payment
then compares the two: the `guest_id` in Booking's answer must be the user from the
guest's token, or it's a 403 — the same ownership check as today.

## 3. What's in a service token

```json
{ "sub": "payment", "type": "service", "exp": "<now + 5 min>" }
```

- **`sub`** — which service, not a user id. Booking checks it against the callers it
  allows for that endpoint (`require_service("payment")`).
- **`type: "service"`** — the same idea as `type: "access"` vs `"refresh"` on user tokens
  (`jwt-basics.md`): a token of the wrong kind is rejected even if its signature is valid.
- **`exp`** — short-lived, see §5.

### Signed, not encrypted

A JWT is **signed**, not encrypted. Anyone can *read* it — the payload is just base64
(paste one into jwt.io) — but nobody can *change or forge* it without the key:

```
Payment:  signature = HMAC(secret, header + payload)    -> sends the token
Booking:  HMAC(secret, header + payload) == signature?   -> yes: really Payment, unchanged
```

So never put anything secret *in* a token — only claims that are fine to be read.

## 4. Its own secret — and who holds it

With **HS256** (what BookIt uses), the **same** secret signs and verifies, so **both**
services hold `SERVICE_TOKEN_SECRET` in their environment. Only the code is split:

| What | Who | Where |
|---|---|---|
| **Create** the token | **Payment**, the caller | `app/payment/booking_client.py`, next to the call that uses it |
| **Verify** it (`require_service`) | **Booking**, the receiver | `app/auth/`, next to `get_current_user` |

Nothing is imported across: the services share the *secret*, not code — the same way
Booking and Notification share the event *format*, not code.

**One secret for every service — and its weak spot.** Whoever holds the secret can sign
a token with **any** `sub`. If Notification held it, it could sign `{"sub": "payment"}`
and pass as Payment. That's why only the services that need it get it: today **just
Payment and Booking** — Notification calls no one, so it's not in its environment.

Once there's a second caller, either fix stops one service from posing as another:

- **One secret per caller** — Booking holds `PAYMENT_TOKEN_SECRET`, `X_TOKEN_SECRET`, …
  and verifies each token with the secret matching its `sub`.
- **RS256** — each caller has its **own private key** (signs only as itself); Booking
  holds their **public** keys (can verify, **can't** forge). Also safer when many services
  verify, since none of them could mint tokens.

**Revisit when:** a second service calls Booking's `/internal` routes, or a third service
needs to verify.

The secret is **not** the `SECRET_KEY` that signs user logins:

- **A guest's token can't pass as a service token** — wrong signature, rejected before
  any claim is even read. `type` is the second line of defense, not the only one.
- **Payment never holds the user-login secret.** If it did, anyone who broke into Payment
  could mint a login token for *any* user. Each service holds only the secrets it needs.
- **Rotating one doesn't touch the other** — changing the service secret doesn't log
  every guest out.

## 5. Short-lived, minted on demand

Five minutes, and Payment mints a fresh one when its current one is about to expire.
There's no login step and no refresh token — Payment holds the secret, so it can always
make a new one. Short-lived means a leaked token is only useful briefly.

## 6. The two rules, both ways

1. **A guest token must not work on `/internal/...`** — otherwise any logged-in guest
   could read any reservation.
2. **A service token must not work on guest routes** — `get_current_user` decodes with
   the user secret and requires `type: "access"`, so a service token fails both checks.

Plus: `/internal/*` is never exposed to the public internet — the gateway (BOOK-67)
doesn't route it.

## 7. The alternative: mTLS

**TLS** (the "S" in HTTPS): when a browser opens `https://bank.com`, only the **server**
proves who it is, with a certificate; the browser checks it, and the connection is
encrypted.

**mTLS** (mutual TLS): **both** sides present a certificate. When Payment connects:

```
Payment ──"here's my certificate: I'm payment"──► Booking
        ◄──"here's mine: I'm booking"────────────
```

Booking knows the caller is Payment from the **connection itself** — no token in the
request at all.

| | Service token (JWT) | mTLS |
|---|---|---|
| The proof | A string in the `Authorization` header | A certificate, part of the connection |
| If it leaks | Whoever has the string can use it until `exp` (5 min) | The private key never leaves the service — nothing in a request to copy |
| Setup | One shared secret + the JWT code BookIt already has | Issue a certificate per service, distribute it, rotate it before it expires |
| Common in | Small and medium systems | Large systems — often automated by a service mesh (e.g. Istio) |

**Chosen: the service token** — two services in one compose file, so the simpler option
that reuses existing code is enough. **Revisit when:** many services, or a real network
where a copied token is a realistic threat.

## 8. One tie left: the guest's own token

`POST /payments` checks the guest with `get_current_user`, which **loads the user from
Booking's `users` table**. After the split Payment can't read it either. Options, to
decide before Payment gets its own database:

- **Payment verifies the guest's JWT itself** — it only needs `sub` (the user id), not
  the user row. But then Payment needs the user-login secret, which §4 just argued
  against (unless tokens move to public-key signing, RS256, where Payment only holds the
  public key).
- **The gateway verifies it** (BOOK-67) and passes the user id on to Payment.
