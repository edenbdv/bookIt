"""BookIt's Notification service (BOOK-64): its own package, its own
dependencies, its own database. Imports nothing from Booking's `app`.

Moved here from app/notification/ in BOOK-64 step 5b-2. Run the consumer
with `python -m notification`, migrations with `python -m notification.migrate`.
"""
