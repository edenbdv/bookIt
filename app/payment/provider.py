"""BOOK-52: a mock payment provider -- no real gateway, simulates the three
outcomes a real one can return. Sandbox behavior mirrors how real providers
structure their own test mode (Stripe's test card numbers deterministically
trigger a decline) -- `force` lets BOOK-53/54 and their tests pin an
outcome; nothing here ever depends on `amount_cents` to decide one.

TIMEOUT is not the same claim as FAILURE, and callers (BOOK-53/54) need to
treat it differently: FAILURE means the provider told us the charge didn't
go through. TIMEOUT means we don't know -- the charge may have succeeded on
the provider's side with the response lost in transit. A saga that treats
a timeout as a clean failure and retries risks a double charge; that's a
BOOK-53 idempotency concern, not something resolved here.

BOOK-55: `correlation_id` is required so a real provider's webhook has
something to echo back later (webhook-basics.md §2). Unused by this mock.
"""

import enum
import random
import uuid


class PaymentOutcome(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


# weighted toward success -- mirrors a real gateway's actual failure rate,
# not a 50/50 coin flip, so exercising the API by hand mostly "just works",
# with failure/timeout still reachable
_OUTCOME_WEIGHTS: dict[PaymentOutcome, float] = {
    PaymentOutcome.SUCCESS: 0.90,
    PaymentOutcome.FAILURE: 0.08,
    PaymentOutcome.TIMEOUT: 0.02,
}


def _resolve_outcome(force: PaymentOutcome | None) -> PaymentOutcome:
    if force is not None:
        return force
    return random.choices(list(_OUTCOME_WEIGHTS), weights=list(_OUTCOME_WEIGHTS.values()), k=1)[0]


# amount_cents is unused by design -- outcome is never derived from it, see module docstring
def charge(
    amount_cents: int, *, correlation_id: uuid.UUID, force: PaymentOutcome | None = None
) -> PaymentOutcome:
    return _resolve_outcome(force)


# BOOK-54's compensating action for a charge that succeeded but couldn't be
# confirmed -- same three-outcome shape as charge(), since a refund attempt
# can itself succeed/fail/timeout against a real provider
def refund(
    amount_cents: int, *, correlation_id: uuid.UUID, force: PaymentOutcome | None = None
) -> PaymentOutcome:
    return _resolve_outcome(force)
