"""Authorization-aware complaint read and update services."""
from datetime import datetime, timezone
import secrets

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .audit import record_audit_event
from .models import ComplaintHistory, Department
from .permissions import has_permission


COMPLAINT_STATUSES = ("Pending", "In Progress", "Resolved")
CATEGORY_DEPARTMENT_CODES = {
    "Electrical": "ELECTRICAL",
    "Plumbing": "PLUMBING",
    "Equipment": "OTHER",
    "Furniture": "OTHER",
    "Network": "NETWORK_IT",
    "Other": "OTHER",
}


class ComplaintNotFoundOrForbidden(LookupError):
    """The complaint does not exist or is outside the caller's permissions."""


class InvalidComplaintUpdate(ValueError):
    pass


def _department_id_for_category(db, category):
    code = CATEGORY_DEPARTMENT_CODES.get(category)
    if code is None:
        return None
    return db.scalar(
        text("SELECT department_id FROM departments WHERE department_code = :code AND active = TRUE"),
        {"code": code},
    )


def _can(db, user, permission_key, category):
    department_id = _department_id_for_category(db, category)
    return has_permission(db, user, permission_key, department_id)


def _row_as_dict(row):
    return dict(row) if row is not None else None


def get_staff_complaint(db, user, complaint_id):
    row = db.execute(
        text("""SELECT complaint_id, name, roll_number, location, category, description,
                         status, priority, created_at, last_updated_at, staff_remarks
                  FROM complaints WHERE complaint_id = :complaint_id"""),
        {"complaint_id": complaint_id},
    ).mappings().one_or_none()
    if row is None or not _can(db, user, "complaint.read_scoped", row["category"]):
        return None
    return _row_as_dict(row)


def get_staff_complaint_history(db, user, complaint_id):
    """Return the timeline only after applying the same scoped complaint read check."""
    complaint = get_staff_complaint(db, user, complaint_id)
    if complaint is None:
        return None
    return list_complaint_history(db, complaint_id, complaint=complaint)


def list_complaint_history(db, complaint_id, *, complaint=None):
    """Return public timeline fields; actor IDs and internal data never leave this service."""
    events = db.scalars(
        select(ComplaintHistory)
        .where(ComplaintHistory.complaint_id == complaint_id)
        .order_by(ComplaintHistory.created_at, ComplaintHistory.id)
    ).all()
    timeline = [
        {
            "event_type": event.event_type,
            "previous_status": event.previous_status,
            "new_status": event.new_status,
            "remarks": event.remarks,
            "created_at": _utc_iso(event.created_at),
        }
        for event in events
    ]
    # A missing submitted event means this complaint predates the history migration.
    if not any(item["event_type"] == "submitted" for item in timeline):
        if complaint is None:
            complaint = db.execute(
                text("SELECT created_at, staff_remarks FROM complaints WHERE complaint_id = :id"),
                {"id": complaint_id},
            ).mappings().one_or_none()
        if complaint is not None:
            timeline.insert(0, {
                "event_type": "submitted",
                "previous_status": None,
                "new_status": None,
                "remarks": None,
                "created_at": _utc_iso(complaint.get("created_at")),
            })
            # A legacy remark has no reliable author or event time. Keep it explicitly undated.
            if complaint.get("staff_remarks") and not events:
                timeline.append({
                    "event_type": "legacy_update",
                    "previous_status": None,
                    "new_status": None,
                    "remarks": complaint["staff_remarks"],
                    "created_at": None,
                })
    return timeline


def _utc_iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        # Legacy complaint timestamps may be naive; display them unchanged rather than
        # implying a timezone that was never stored.
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def list_staff_complaints(db, user, *, status="", search=""):
    rows = db.execute(
        text("""SELECT complaint_id, name, roll_number, location, category, description,
                         status, priority, created_at, last_updated_at, staff_remarks
                  FROM complaints ORDER BY created_at DESC, id DESC""")
    ).mappings().all()
    needle = search.strip().casefold()
    visible = []
    for row in rows:
        if not _can(db, user, "complaint.read_scoped", row["category"]):
            continue
        if status and row["status"] != status:
            continue
        if needle and not any(
            needle in str(row.get(field) or "").casefold()
            for field in ("complaint_id", "name", "roll_number", "location", "category")
        ):
            continue
        visible.append(_row_as_dict(row))
    return visible


def update_complaint_status(db, user, complaint_id, status, remarks="", request_key=None):
    if request_key is None:
        request_key = secrets.token_urlsafe(24)
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 80:
        raise InvalidComplaintUpdate("The update request is invalid. Reload the complaint and try again.")
    if status not in COMPLAINT_STATUSES:
        raise InvalidComplaintUpdate("Choose a valid complaint status.")
    if not isinstance(remarks, str) or len(remarks) > 2000:
        raise InvalidComplaintUpdate("Staff remarks must be 2,000 characters or fewer.")
    complaint = get_staff_complaint(db, user, complaint_id)
    if complaint is None:
        raise ComplaintNotFoundOrForbidden

    category = complaint["category"]
    if not _can(db, user, "complaint.triage", category):
        raise PermissionError("You do not have permission to update this complaint.")
    if status == "Resolved" and not _can(db, user, "complaint.resolve_review", category):
        raise PermissionError("You do not have permission to mark this complaint resolved.")

    # A key already committed by an earlier attempt makes an HTTP retry a no-op.
    request_keys = (request_key, f"{request_key}:status", f"{request_key}:remark")
    prior_complaint_ids = set(db.scalars(
        select(ComplaintHistory.complaint_id).where(ComplaintHistory.request_key.in_(request_keys))
    ).all())
    if prior_complaint_ids:
        if prior_complaint_ids != {complaint_id}:
            raise InvalidComplaintUpdate("The update request is invalid. Reload the complaint and try again.")
        return get_staff_complaint(db, user, complaint_id)

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    clean_remarks = remarks.strip()
    status_changed = status != complaint["status"]
    if status_changed or clean_remarks:
        next_remarks = clean_remarks or complaint.get("staff_remarks")
        try:
            result = db.execute(
                text("""UPDATE complaints
                        SET status = :status, staff_remarks = :remarks, last_updated_at = :updated_at
                      WHERE complaint_id = :complaint_id"""),
                {
                    "status": status,
                    "remarks": next_remarks,
                    "updated_at": now,
                    "complaint_id": complaint_id,
                },
            )
            if result.rowcount != 1:
                db.rollback()
                raise ComplaintNotFoundOrForbidden

            if status_changed:
                db.add(ComplaintHistory(
                    complaint_id=complaint_id,
                    event_type="resolved" if status == "Resolved" else "status_changed",
                    previous_status=complaint["status"],
                    new_status=status,
                    actor_user_id=user.user_id,
                    created_at=now_dt,
                    request_key=f"{request_key}:status",
                ))
            if clean_remarks:
                db.add(ComplaintHistory(
                    complaint_id=complaint_id,
                    event_type="staff_update",
                    remarks=clean_remarks,
                    actor_user_id=user.user_id,
                    created_at=now_dt,
                    request_key=f"{request_key}:remark",
                ))
            record_audit_event(
                db,
                actor_user_id=user.user_id,
                action="complaint.status_updated" if status_changed else "complaint.remark_added",
                subject_type="complaint",
                subject_id=complaint_id,
                details={"from_status": complaint["status"], "to_status": status,
                         "remark_added": bool(clean_remarks)},
                occurred_at=now_dt,
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            # A concurrent retry may have inserted the same unique key after our check.
            already_recorded = set(db.scalars(
                select(ComplaintHistory.complaint_id).where(
                    ComplaintHistory.request_key.in_(request_keys)
                )
            ).all())
            if not already_recorded:
                raise
            if already_recorded != {complaint_id}:
                raise InvalidComplaintUpdate("The update request is invalid. Reload the complaint and try again.")
    return get_staff_complaint(db, user, complaint_id)
