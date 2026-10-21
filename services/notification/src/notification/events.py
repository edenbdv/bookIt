"""BOOK-64 step 5a: the booking event types Notification listens for.

Its own constants, not an import of Booking's OutboxEventType: separate
services don't share code, they share a contract -- these exact strings,
which Booking writes as `event_type` in every payload
(app/core/outbox/models.py). If the two ever drift, Notification skips the
unknown type (consumer.py) and no email goes out -- which the real-Kafka
pipeline tests (tests/integration/test_notification_pipeline.py) catch.
"""

BOOKING_CREATED = "booking_created"
BOOKING_CONFIRMED = "booking_confirmed"
BOOKING_CANCELLED = "booking_cancelled"
