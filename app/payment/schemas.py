import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.payment.models import PaymentKind, PaymentStatus


class PaymentCreate(BaseModel):
    reservation_id: uuid.UUID
    # no id, no kind, no amount_cents, no status, no created_at: a client
    # can only ever initiate a charge, so kind is implicitly CHARGE, never
    # a client-chosen value. REFUND rows are created by the server's own
    # saga compensation on cancellation (app/README.md), not by a client
    # request. amount_cents is read from Reservation.total_cents
    # server-side -- letting the client set the amount it pays would
    # defeat the whole point of charging it. status starts PENDING until
    # the mock provider responds


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reservation_id: uuid.UUID
    kind: PaymentKind
    amount_cents: int
    status: PaymentStatus
    created_at: datetime


# no PaymentUpdate: a payment's status moves PENDING -> SUCCEEDED/FAILED
# only via the mock provider's response, and refunds are their own new
# Payment row (kind=REFUND), not an edit to the original charge. Nothing
# about an existing payment is meant to be client-editable.
