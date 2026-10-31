from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.selectable import TableClause

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


@event.listens_for(Session, "do_orm_execute")
def reject_audit_bulk_mutation(execute_state):
    """Block session bulk UPDATE/DELETE against audit_logs, including aliased targets."""
    if not (execute_state.is_update or execute_state.is_delete):
        return

    mapper = execute_state.bind_mapper
    if mapper is not None and mapper.class_ is AuditLog:
        raise ValueError("Audit events are append-only.")

    target = getattr(execute_state.statement, "table", None)
    if target is None:
        return

    # DML may target the mapped Table directly, a lightweight table() expression,
    # or an Alias wrapping one of those. Walk only the DML target tree (not the
    # WHERE/RETURNING expressions) and fail closed on the physical table name.
    if any(
        isinstance(element, TableClause) and element.name == AuditLog.__tablename__
        for element in visitors.iterate(target)
    ):
        raise ValueError("Audit events are append-only.")

