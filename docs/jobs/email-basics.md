# Sending email from an app, in the amount of depth BOOK-45 needs

Deliberately general — no BookIt specifics. For what this project chose (MailHog for
local dev, a real provider deferred) and why, see `worker-decisions.md`. Scoped to the
two paths this project will actually take: a local mail catcher now, a transactional
provider later. Running your own mail server isn't covered — see §2 for why that's not
a realistic third option.

## 1. The problem: your app is not in the business of delivering email

Sending an email isn't "connect and transmit" — the hard part starts *after* that
succeeds. A receiving mail server (Gmail, Outlook, whatever runs `@company.com`)
decides for itself whether to put your message in the inbox, the spam folder, or drop
it silently, based on the sending IP's history and reputation — something that has
nothing to do with your code being correct. `250 OK: queued` from an SMTP call means
*accepted for delivery*, not delivered; the inbox/spam decision happens afterward,
invisibly to the sending side.

**This is why "deliverability" is a separate problem from "sending"** — and why nobody
builds their own answer to it if they can avoid it (§2).

## 2. Two realistic paths — and why "run your own server" isn't a third

```
Local dev                          Later, in production

app ──SMTP──▶ mail catcher         app ──SMTP or HTTPS──▶ provider ──▶ recipient's
              (never delivers                             mail server
               anywhere)
```

**A transactional email provider (SES, Postmark, SendGrid, Mailgun, ...) sells you
their sending reputation.** Their IPs have years of clean sending history, the
whitelisting that took time to earn, and infrastructure for bounce/complaint handling
already built. Pointing your app at them — either over SMTP with their credentials, or
their HTTP API — means inheriting that reputation instead of starting from zero.

**Running your own SMTP server (Postfix, etc.) means owning all of that yourself,
starting from zero, indefinitely:** a brand-new sending IP has no reputation and gets
treated as suspicious by default; you'd need your own SPF/DKIM/DMARC DNS records
correctly configured just to be considered legitimate; one misconfiguration or one
compromised account sending spam through it tanks deliverability for everything after.
This is real, ongoing operational work with no relation to what the app itself does —
worth knowing it's *possible*, but there's no reason to choose it when a provider exists
specifically to make this someone else's problem.

**Provider, SMTP relay vs. HTTP API** — a real distinction worth knowing when the time
comes to pick one, since both point at the same provider and same reputation:

| | SMTP relay | HTTP API |
|---|---|---|
| Auth | Username/password over SMTP | API key in a header |
| Errors | SMTP reply codes (`550`, `421`) — a text protocol, not built for structured errors | JSON error bodies — which recipient failed, why |
| Delivery events (bounced, opened, complained) | Provider emails you a bounce notice, or you poll | Webhooks — the provider calls your app back |

Neither is "more correct." SMTP relay is the smaller change if the code already speaks
SMTP (true here — the same client code that talks to a local mail catcher today can
point at a provider's SMTP relay later with just a host/credentials change). The API is
the better fit once bounce/complaint handling or templating matters enough to justify
the switch.

## 3. The local mail catcher (MailHog/Mailpit) — a test double, not a smaller server

```
app ──SMTP──▶ mail catcher ──▶ (nowhere — held locally, viewable in a browser)
```

It speaks enough of the SMTP protocol to accept a message, then holds it and shows it
in a local web UI instead of forwarding it anywhere. **No reputation, no DNS records, no
deliverability concerns — because nothing is ever actually delivered.** Comparable to
running a real Postgres in a container for tests instead of mocking the DB driver: same
protocol, same client code, none of the production concerns.

**What it buys, concretely:**

- Every send during development is inspectable, without spamming a real inbox on every
  test run.
- Failure is triggerable on demand — stop the container, and every send fails with a
  real connection error, the same shape a genuine outage would produce.
- No account, no API key, no rate limit to work around while iterating.

**What it can't tell you:** anything about deliverability — that's out of scope by
design, deferred to whichever provider eventually gets connected (§2).

## 4. What this means for retry design later

A provider (or the local catcher standing in for one) can fail in shapes worth naming
now, since a later retry policy is designed against exactly these:

- **Connection failure** — can't reach the server at all. Always worth retrying.
- **Temporary rejection (SMTP `4xx` / provider HTTP `5xx` or `429`)** — "try again
  later," e.g. rate-limited or momentarily overloaded. Worth retrying, with backoff.
- **Permanent rejection (SMTP `5xx` / provider HTTP `4xx` other than `429`)** — e.g. an
  invalid recipient address. Retrying identically fails identically — this needs a
  different response, not more attempts.

The rule this feeds — retry transient failures, not permanent ones — isn't
email-specific, but email is where it first shows up as a real question.

---

## Cheat sheet

| Term | Meaning |
|---|---|
| `250 OK: queued` | Accepted for delivery, not delivered — the inbox/spam decision happens later, invisibly |
| Transactional email provider | Sells you their sending reputation (SES, Postmark, SendGrid, Mailgun) instead of you building your own |
| SMTP relay vs. HTTP API (same provider) | Same reputation either way; API adds structured errors and webhooks; SMTP relay is a smaller change from a mail-catcher setup |
| Mail catcher (MailHog/Mailpit) | Local SMTP test double — accepts and displays, never delivers, no reputation to manage |
| Transient vs. permanent failure | Transient (timeout, `429`, `5xx`) → retry. Permanent (bad address) → don't |
