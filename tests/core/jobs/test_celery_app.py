from app.core.jobs.celery_app import app


def test_expire_pending_reservations_is_scheduled() -> None:
    entry = app.conf.beat_schedule["expire-pending-reservations"]

    assert entry["task"] == "app.worker.expire_pending_reservations"
    assert entry["schedule"] == 60.0
