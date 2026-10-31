from sqlalchemy import event

from .models import AuditLog


def record_audit_event(
    db,
    *,
    action,
    subject_type,
    subject_id,
    actor_user_id=None,
    outcome="success",
    request_id=None,
    details=None,
    occurred_at=None,
):
    """Add a redacted audit event; caller controls the surrounding transaction."""
    event_row = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        subject_type=subject_type,
        subject_id=str(subject_id),
        outcome=outcome,
        request_id=request_id,
        details=details or {},
        occurred_at=occurred_at,
    )
    db.add(event_row)
    return event_row


@event.listens_for(AuditLog, "before_update")
def reject_audit_update(_mapper, _connection, _target):
    raise ValueError("Audit events are append-only.")


@event.listens_for(AuditLog, "before_delete")
def reject_audit_delete(_mapper, _connection, _target):
    raise ValueError("Audit events are append-only.")

