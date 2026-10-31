# Campus Maintenance Management System: Role and Permission Matrix

## Authorization rules

This matrix is a default proposal for review with IIIT Dharwad operations. Permissions are enforced in Flask services/routes and database queries; UI hiding is only presentation. Every request must pass both the action check and the resource scope check. Unknown roles, inactive assignments, expired invitations, and missing department scope deny by default.

“Assigned department” means an active staff role assignment, not a department ID supplied by the browser. Technician access is narrower still: work orders assigned to that technician, plus explicitly allowed departmental work-order fields. Multi-department staff receive one scoped grant per department. All privileged changes write an audit event.

## Role matrix

| Capability | Student | Super Admin | Maintenance Head | Department Supervisor | Technician |
| --- | --- | --- | --- | --- | --- |
| Submit complaint via QR or portal | Own submissions | Yes, when assisting; actor recorded | Yes, when assisting; actor recorded | Yes, when assisting; actor recorded | Yes, when assisting; actor recorded |
| View public receipt/status | Own complaint using account or valid tracking capability | All complaints | All complaints | Complaints in assigned departments; public fields only as needed | Assigned work orders and necessary complaint fields |
| View reporter identity/contact | Own only | Yes, only for legitimate administration | Yes, for operational handling | Assigned department cases | Only assigned work requiring contact; minimize fields |
| Create/manage staff accounts and invite/reset | No | Yes, including role/department grants; audit every action | No | No | No |
| Manage departments, categories, location tree, routing rules | No | Yes | Propose/approve operational routing per agreed governance; no identity administration | Propose department routing; no global settings | No |
| Triage category/severity and initial route | No; may add factual comment | Yes | Yes, across departments | Yes, within assigned department/triage queue | No; may flag risk to supervisor |
| View department queue | No | All | All | Assigned department(s) | Only assigned work orders |
| Create/reassign work order across departments | No | Yes | Yes; cross-department | Within assigned department; escalate cross-department reassignment | No |
| Assign/reassign technician | No | Yes | Yes | Within assigned department | No |
| Update work status, notes, evidence | No | Administrative override only with reason | Yes, operational override with reason | Yes within assigned department | Assigned work order only; cannot directly overwrite complaint state |
| Resolve/close complaint | Confirm or reopen own complaint under policy; no staff workflow edits | Exceptional override with reason | Cross-department resolution approval/escalation | Department resolution review; cannot bypass required checks | Mark assigned work ready for review; supervisor/head completes complaint transition |
| Duplicate link or cancel | Request duplicate/cancellation | Yes, audited | Yes, audited | Within scope, audited | Suggest duplicate; cannot merge/cancel |
| SLA and escalation | See student-facing progress; submit urgent follow-up | Configure global policy | View/manage operational SLA and escalation across departments | View/manage own department escalation | See own work deadline; request blocker/escalation |
| Analytics and reports | Own complaint history/feedback only | Global | Cross-department operational | Assigned department only | Assigned queue/workload only |
| Audit log | No | Restricted security/administrative audit | Operational audit for authorized scope | Department-scoped events where needed | No general audit search |
| Manage own profile/security | Own profile, notification preferences, logout, recovery | Own profile; plus account administration | Own profile | Own profile | Own profile |

### Scope and redaction constraints

- A student can access a complaint only through their authenticated account association or a valid, unexpired, revocable capability token. Roll number alone is never sufficient. The student cannot read internal notes, staff contact details, unrelated complainants, security audit events, or attachment objects outside the complaint.
- Supervisors are department-scoped on both list and detail endpoints. Guessing another department's complaint or work-order UUID must return a non-disclosing not-found/denied response and must not reveal whether the record exists.
- Technicians cannot browse all complaints in a department by default. They receive only assigned work and fields needed to perform it. An explicit additional dispatch permission may be approved if operations require it.
- Maintenance Heads can access cross-department operational records but do not gain account/role administration by implication. Super Admin is the only default account/role administrator.
- Global Super Admin data access is itself audited. Consider break-glass reason capture for sensitive complaint descriptions and attachment downloads.
- List endpoints, exports, statistics, PDFs/receipts, downloads, notification APIs, and future JSON endpoints use the same policy service. Aggregate reports must avoid leaking individual records to out-of-scope users.

## Permission keys to implement

Use explicit stable permission keys rather than branching on display labels. Initial examples:

| Permission key | Description |
| --- | --- |
| `complaint.submit` | Create a complaint with validated QR/location context. |
| `complaint.read_own` | Read a complaint linked to the authenticated student or valid capability. |
| `complaint.read_scoped` | Read operational fields for a complaint within an authorized scope. |
| `complaint.triage` | Set category, severity, route, and triage outcome. |
| `complaint.reassign` | Change current department or escalation route with a reason. |
| `complaint.resolve_review` | Accept or reject a resolution and set complaint workflow state. |
| `complaint.close_override` | Exceptional close/cancel with a mandatory reason and audit event. |
| `complaint.reopen_own` | Reopen an eligible own complaint within policy. |
| `work_order.read_assigned` | Read work orders assigned to the current technician. |
| `work_order.manage_department` | Create, prioritize, assign, and update work in an assigned department. |
| `work_order.update_assigned` | Update only the current technician's assigned work orders. |
| `department.manage` | Create, update, or archive departments and routing configuration. |
| `staff.invite` | Invite a user and set an approved role/department assignment. |
| `staff.manage` | Suspend/revoke accounts and assignments; reset/revoke sessions. |
| `sla.manage` | Configure effective-dated SLA policies. |
| `report.read_global` / `report.read_department` | Read the corresponding aggregate/detail report scope. |
| `audit.read` | Read restricted audit events within an explicitly granted scope. |

Do not grant `complaint.close_override`, `staff.manage`, or `department.manage` as part of a generic “staff” role.

## Role assignment lifecycle

1. A Super Admin creates an invitation with one role and an approved department scope (unless the role is global). The system stores only a token hash, expiry, creator, and status.
2. For the initial pilot, the invitee redeems once and establishes an individual password for the institutional email address. SSO is deferred until institutional integration availability is confirmed. Redemption cannot accept a client-selected role or scope.
3. Super Admin may add another scoped assignment, suspend an account, or revoke one assignment. Revocation takes effect on the next request through session-version/active-assignment checks.
4. Every invitation, redemption, role/scope change, suspension, reset, override, and failed privileged action creates a redacted audit event.
5. Staff departures revoke sessions and assignments promptly. Account deletion is not used to erase attribution from prior work.

## Complaint lifecycle and transition authority

### Proposed flow

1. **Submitted:** student or QR user submits a validated complaint. System creates a permanent complaint ID, receipt, timeline event, initial severity/priority suggestion, and secure tracking capability. Emergency indicators show immediate guidance and notify the configured dispatch path; keyword detection alone is not a safety guarantee.
2. **Triage:** Maintenance Head or a Supervisor with triage permission reviews category, severity, location, duplicate candidates, and department. Uncertain routing stays in a visible unassigned queue. A duplicate links to a canonical complaint while preserving both IDs and histories.
3. **Assigned:** a department owns the complaint. Supervisor or Maintenance Head creates one or more work orders and assigns technicians. Assignment events are recorded separately from current assignment.
4. **Accepted / in progress:** technician accepts, starts, or marks work blocked with a reason. Each allowed transition is recorded with actor and UTC timestamp. SLA clocks and escalation notices are calculated from a versioned policy.
5. **Ready for review:** technician records work performed, resolution notes, and optional evidence; marks the work order ready. This is not an unreviewed direct update to the complaint's final status.
6. **Resolution review:** Supervisor or Maintenance Head checks the work, coordinates all open work orders, and records resolution. Student receives an in-app/approved notification when available. The complaint stays auditable if the student does not respond.
7. **Student confirmation:** student confirms resolution and may provide feedback. The complaint becomes `closed`. If no response arrives by the approved policy deadline, a policy-controlled auto-close may occur with an explicit system actor/event; never imply the student confirmed.
8. **Reopen:** student can request reopening within a configured window and provide a reason/evidence. The system reopens the complaint or creates a linked follow-up work order according to policy. After the window, a new complaint can link to the prior record. Staff can also reopen with an audited reason.
9. **Escalation/cancellation:** a Supervisor escalates outside department authority to Maintenance Head. Super Admin handles administrative exceptions. Cancellation requires a reason and leaves the original record intact. Emergency workflow can bypass normal queue order but not authorization or audit.

### Transition guardrails

- Only service methods perform state transitions; requests provide intent, not arbitrary `status` strings.
- Reject invalid/out-of-order transitions with a clear conflict response. Use optimistic version checks to prevent silent overwrites by concurrent users.
- Every transition writes current state and a history event in the same database transaction. Insert notification outbox rows in that transaction too.
- Student-visible timeline entries use an explicit visibility field and safe summaries, not staff-only notes.
- Reassignment ends one active assignment and creates the next with actor/reason; never rewrite old events.
- Duplicate marking does not delete or merge complaint content. Preserve the original complaint ID, receipt, attachments, and history; restrict edits on duplicate records according to an approved policy.

