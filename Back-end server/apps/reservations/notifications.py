"""
apps/reservations/notifications.py

Thin glue between reservations (views.py, services.py) and the Celery
tasks in apps.notifications.tasks — same role and pattern as
borrow_transactions/notifications.py. Nothing in reservations imports
Celery or the task module directly except through here.
"""
from django.db import transaction

from apps.notifications.tasks import (
    send_reservation_created_email,
    send_reservation_cancelled_email,
    send_reservation_ready_email,
    send_reservation_expired_email,
)


def notify_reservation_created(reservation_id):
    transaction.on_commit(lambda: send_reservation_created_email.delay(str(reservation_id)))


def notify_reservation_cancelled(reservation_id):
    transaction.on_commit(lambda: send_reservation_cancelled_email.delay(str(reservation_id)))


def notify_reservation_ready(reservation_id):
    transaction.on_commit(lambda: send_reservation_ready_email.delay(str(reservation_id)))


def notify_reservation_expired(reservation_id):
    transaction.on_commit(lambda: send_reservation_expired_email.delay(str(reservation_id)))