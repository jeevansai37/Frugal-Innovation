from sqlalchemy import or_, select

from .models import Department, Permission, Role, RolePermission, StaffRoleAssignment, utc_now


ROLE_NAMES = {
    "SUPER_ADMIN": "Super Admin",
    "MAINTENANCE_HEAD": "Maintenance Head",
    "DEPARTMENT_SUPERVISOR": "Department Supervisor",
    "TECHNICIAN": "Technician",
    "STUDENT": "Student",
}

PERMISSION_DESCRIPTIONS = {
    "complaint.submit": "Submit a campus maintenance complaint.",
    "complaint.read_own": "Read a complaint linked to the student or tracking capability.",
    "complaint.read_scoped": "Read operational complaint fields within authorized scope.",
    "complaint.triage": "Triage a complaint within authorized scope.",
    "complaint.reassign": "Change complaint routing within authorized scope.",
    "complaint.resolve_review": "Review and record complaint resolution.",
    "complaint.close_override": "Override a complaint closure with an audited reason.",
    "complaint.reopen_own": "Request reopening of an eligible own complaint.",
    "work_order.read_assigned": "Read work orders assigned to this technician.",
    "work_order.manage_department": "Manage work orders within an assigned department.",
    "work_order.update_assigned": "Update work orders assigned to this technician.",
    "department.manage": "Manage campus departments and routing configuration.",
    "staff.invite": "Invite staff and grant approved roles and scopes.",
    "staff.manage": "Suspend staff accounts or revoke role assignments.",
    "sla.manage": "Manage effective-dated service level policies.",
    "report.read_global": "Read cross-department operational reports.",
    "report.read_department": "Read reports in an assigned department.",
    "audit.read": "Read restricted audit events within authorized scope.",
}

ROLE_PERMISSIONS = {
    "SUPER_ADMIN": set(PERMISSION_DESCRIPTIONS),
    "MAINTENANCE_HEAD": {
        "complaint.read_scoped", "complaint.triage", "complaint.reassign",
        "complaint.resolve_review", "complaint.close_override",
        "work_order.manage_department", "department.manage", "sla.manage",
        "report.read_global", "audit.read",
    },
    "DEPARTMENT_SUPERVISOR": {
        "complaint.read_scoped", "complaint.triage", "complaint.reassign",
        "complaint.resolve_review", "work_order.manage_department",
        "report.read_department",
    },
    "TECHNICIAN": {
        "complaint.read_scoped", "work_order.read_assigned", "work_order.update_assigned",
    },
    "STUDENT": {"complaint.submit", "complaint.read_own", "complaint.reopen_own"},
}

GLOBAL_ROLES = {"SUPER_ADMIN", "MAINTENANCE_HEAD"}
DEPARTMENT_ROLES = {"DEPARTMENT_SUPERVISOR", "TECHNICIAN"}


def has_permission(db, user, permission_key, department_id=None):
    """Return False for unknown/inactive roles, permissions, or missing scope."""
    if user is None or user.status != "active" or user.email_verified_at is None:
        return False
    permission = db.get(Permission, permission_key)
    if permission is None:
        return False
    if user.account_kind == "student":
        role_keys = {"STUDENT"}
        scoped_role_keys = set()
    elif user.account_kind == "staff":
        assignments = db.scalars(
            select(StaffRoleAssignment).where(
                StaffRoleAssignment.user_id == user.user_id,
                StaffRoleAssignment.revoked_at.is_(None),
                or_(StaffRoleAssignment.expires_at.is_(None), StaffRoleAssignment.expires_at > utc_now()),
            )
        ).all()
        role_keys = set()
        scoped_role_keys = set()
        for assignment in assignments:
            role = db.get(Role, assignment.role_key)
            if role is None or not role.active:
                continue
            grant = db.get(RolePermission, (assignment.role_key, permission_key))
            if grant is None:
                continue
            if assignment.role_key in GLOBAL_ROLES:
                role_keys.add(assignment.role_key)
            elif department_id is not None and assignment.department_id == department_id:
                department = db.get(Department, department_id)
                if department is not None and department.active:
                    scoped_role_keys.add(assignment.role_key)
    else:
        return False

    for role_key in role_keys:
        role = db.get(Role, role_key)
        if role is not None and role.active and db.get(RolePermission, (role_key, permission_key)):
            return True
    for role_key in scoped_role_keys:
        return True
    return False


def is_super_admin(db, user):
    if (user is None or user.account_kind != "staff"
            or user.status != "active" or user.email_verified_at is None):
        return False
    return db.scalar(
        select(StaffRoleAssignment.assignment_id)
        .join(Role, Role.role_key == StaffRoleAssignment.role_key)
        .where(
            StaffRoleAssignment.user_id == user.user_id,
            StaffRoleAssignment.role_key == "SUPER_ADMIN",
            StaffRoleAssignment.department_id.is_(None),
            StaffRoleAssignment.revoked_at.is_(None),
            or_(StaffRoleAssignment.expires_at.is_(None), StaffRoleAssignment.expires_at > utc_now()),
            Role.active.is_(True),
        )
        .limit(1)
    ) is not None


def validate_role_scope(db, role_key, department_id):
    role = db.get(Role, role_key)
    if role is None or not role.active:
        raise ValueError("Unknown or inactive role.")
    if role_key in GLOBAL_ROLES:
        if department_id is not None:
            raise ValueError("This role must not have a department scope.")
    elif role_key in DEPARTMENT_ROLES:
        department = db.get(Department, department_id) if department_id else None
        if department is None or not department.active:
            raise ValueError("An active department is required for this role.")
    else:
        raise ValueError("This role cannot be assigned as a staff role.")

