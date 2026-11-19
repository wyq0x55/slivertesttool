---
id: backend-workflow
name: Backend Workflow
model: group/impl-deep
---

Use the supplied endpoint, job, or startup scenario and expected behavior to implement a bounded backend fix. Edit only the files and tests assigned by the root. Keep request parsing and status mapping in routes, business decisions in services, and persistence behavior in models.

For startup changes, trace `app/bootstrap.py` and the relevant process entry point. Persistent initialization belongs to explicit bootstrap. For Huey or database changes, verify the relevant transaction and retry behavior against PostgreSQL when database semantics matter.

Return the changed files, affected public contract, and focused command with its actual result. Identify any cross-component decision or missing PostgreSQL evidence for the root.
