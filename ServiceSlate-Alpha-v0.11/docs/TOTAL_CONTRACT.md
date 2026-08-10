# ServiceSlate — Concise Total Contract

## 1. North star

ServiceSlate is **Operational Memory Software**. It should remove administrative remembering and duplicate entry rather than move them between employees.

**Technician records facts once → ServiceSlate saves/connects/checks them → coordinator reviews exceptions → future staff inherit useful history.**

Routine workflows must be understandable without training; treat hesitation by a first-time user as a UX defect.

## 2. Product shape

One shared platform kernel supports isolated organization profiles.

V1 profiles:
- Automotive Equipment Sales & Service — primary
- Pet Grooming — secondary

Profile choice is stored per organization. Disabled profile concepts must not leak through navigation, APIs, search, forms, reports, or background work.

## 3. Deployment

Alpha runs on one ordinary Windows laptop/business PC with SQLite and local files. No dedicated server, NAS, Docker, paid API, cloud account, or database administration is required.

Future hosting may replace SQLite/local files with PostgreSQL/company storage behind the same domain/API/sync contracts. That is an infrastructure migration, not a workflow rewrite.

## 4. Data truth

- One permanent identity per business record.
- Edits update current truth and append history; do not clone records.
- Operational deletion means archive/cancel/void/supersede/merge where practical.
- Commands are idempotent so retry/double-click does not duplicate business effects.
- Mutable records use optimistic concurrency; no blind last-save-wins.
- Submitted/signed/revision evidence is immutable; correction creates a revision/history event.
- External-system failure never rolls back or falsely reports core ServiceSlate work.
- The organization can export its data.

## 5. Low-friction UX

Every routine screen answers:
1. What am I looking at?
2. What needs attention?
3. What should I do next?

Use one obvious primary action, progressive disclosure, inline creation, safe defaults, plain language, autosave, clear sync state, and errors that explain what happened, whether work is safe, and the next action.

Visual language: dark graphite/slate, muted steel blue, restrained status color, subtle depth, generous spacing, strong hierarchy, no flashy SaaS/glass/neon styling.

## 6. Automotive operating model

Customer hierarchy:
**Account/parent → location → service area/bay → permanent equipment.**

Jobs may contain multiple visits and return work. Large installations may later use project/work-package structure without breaking job/equipment identity.

Equipment history includes manufacturer/model/serial, location, install/warranty, inspections, measurements, parts, photos/files, recommendations, repairs, and replacement relationships.

Completed service should structure:
**Complaint → Finding → Cause if known → Correction → Verification**, plus internal notes vs customer-facing report.

Technician outcomes include completed, need parts, return visit, second technician, more diagnostic time, manufacturer information, customer unavailable, inaccessible equipment, additional authorization, safety restriction, or unable to reproduce.


## 6A. Customer service-day awareness

Normal automotive scheduling promises the **service day**, not an internal technician start time. A sent or opened message is not confirmation. ServiceSlate tracks explicit customer acknowledgement by secure link, phone, or in person; preserves the exact notice/date acknowledged; and makes that acknowledgement stale when the scheduled date changes. Customer wording should set expectations confidently without implying the company expects to miss appointments.

## 7. Coordinator Assist

Coordinator Assist is a state-derived work queue, not a second manual task list.

It surfaces items such as:
- new/untriaged requests
- approved unscheduled work
- customer contact/follow-up
- parts-blocked/parts-ready work
- recurring/inspection due work
- technician submissions needing review
- completed but not billing-ready work
- integration/ownership exceptions

Every open job must have a meaningful state, owner/next action, review point, priority, and blocker when applicable.

## 8. Compass

Compass is explainable scheduling/assignment intelligence, **not** delivery routing or employee tracking.

Hard constraints include commitments, availability, qualifications, crew, parts/resources, dependencies, access, authorization/forms where required, and work limits. If none fit, return **No feasible schedule** rather than breaking a hard rule.

Soft objectives include geography, capacity/day fill, overtime, priority, aging, schedule stability, nearby-work grouping, and resource readiness.

Crew supports:
- one technician
- two technicians for the whole visit
- two technicians for only a defined overlap
- lead + helper
- qualification combinations

Historical duration/crew patterns may make explainable suggestions. They never silently weaken safety/company/manufacturer rules.

## 9. Vehicles

Vehicles are operational resources: unit, branch, assigned tech, availability, equipment/stock, approved overnight/remote-start exception.

Normal origin is the assigned branch. A rare remote start is explicitly approved for that workday and may use a deliberately entered planning point.

ServiceSlate contains **no GPS, dash-cam, route history/replay, speed, geofencing, driver score, or continuous employee tracking**.

Soft truck inventory answers “what should probably be on this truck?” without forcing warehouse-grade scans for every small consumable.

## 10. Technician Companion

Browser first; optional PWA install; native Android/iOS only later if browser limitations justify it. Every future native client must use the same APIs, commands, IDs, permissions, forms, and sync model.

Field data is written immediately to durable device storage, queued with permanent operation IDs, and synchronized when central ServiceSlate is reachable. Closing/reopening the browser must restore work. Retries must be safe.

Technician sees:
- assigned/current work
- customer/location/equipment context
- approved/locked historical company work relevant to diagnosis
- manuals/files/templates/context made available

Technician does **not** need another technician's live draft, unsubmitted paperwork, live schedule, or tracking data.

A shared device must scope cached work to the signed-in organization/account.

## 11. Coordinator review

Coordinator is the final data-quality failsafe, not the primary entry clerk.

Clean technician submissions should be quick approval. ServiceSlate checks missing fields and contradictions. Substantive corrections return to the technician or create an auditable correction; coordinators do not silently rewrite technical evidence.

Approval automatically updates job/customer/location/equipment/parts/recommendation/recurring/search/readiness history as appropriate. No manual reconstruction.

## 12. Parts, recurring, estimates, forms, inspections

- Parts holds and states are first-class; all-required-parts-ready should create an actionable return-work cue.
- Recurring plans create future obligations, not pretend calendar appointments.
- Recommendations never disappear merely because declined/deferred.
- Estimates are revisioned; approval applies to the exact revision shown.
- Forms are controlled/versioned; published/completed evidence preserves exact wording/version.
- Acknowledgement, consent, authorization, and signature are distinct concepts.
- Inspections are structured/versioned with measurements, deficiencies, evidence, result, next due, and immutable completed history. ServiceSlate does not claim regulatory/legal compliance simply because a checklist was completed.

## 13. Customer communication/portal

ServiceSlate owns communication history and workflow state. Actual email/SMS/calendar transport is an adapter.

If transport is unavailable, say **Prepared/Not connected**, never **Sent**.

Low-friction customer links may support exact-revision estimates/forms/authorization without requiring an account. Production hosting must secure token lifetime, transport, rate limits, logging, and legal evidence appropriately.

## 14. Grooming profile

Purpose-built grooming concepts only:
- customer
- pet/profile/safety/handling
- groomer
- grooming service
- appointment/resource
- forms/waivers
- waitlist
- rebooking obligation
- history

Cancellation may surface eligible waitlist matches; completion may create a rebooking obligation. Neither automatically contacts/books a customer without approved workflow.

## 15. Migration/integrations

Legacy import follows:
**Read → Stage → Normalize → Validate → Detect duplicates → Review → Commit.**

Preserve provenance, never guess ambiguous matches, and distinguish missing/unavailable/unreadable data.

FastField/NoteWise/customer CSV import is allowed from data the user is authorized to access. QuickBooks migration must not require bypassing permissions or altering the hosted company file.

Outlook/Microsoft 365, email/SMS, QuickBooks, public enrichment, hosted database/storage, and OCR are optional adapters—not foundations.

## 16. Security/privacy/recovery

- Established libraries for hashing, validation, cryptography/TLS; no DIY crypto.
- Authorization enforced by backend, not only hidden buttons.
- Sensitive data minimized in logs/caches.
- Field cache expires/clears by policy after synchronization/need ends.
- Backup creation, verification, restore safety copy, and export are first-class.
- Production internet deployment requires dedicated auth/session/rate-limit/secret/monitoring/backup hardening.
- WCAG 2.2 AA is the accessibility target.

## 17. Explicit non-goals

Not V1 product scope:
- GPS/dash-cam/driver monitoring
- payroll/full accounting
- payment processing
- full ERP/warehouse valuation
- marketing automation
- autonomous customer communication
- autonomous schedule changes
- delivery-style route optimization
- generic AI chatbot everywhere

## 18. No-guess implementation rule

Implementation may not invent product behavior, fields, statuses, permissions, integration truth, retention, or terminology. If a genuine uncovered contradiction materially affects behavior, stop that affected path and produce a **Decision Needed** item rather than guessing.

## 19. Alpha acceptance proof

A real demonstration should complete:

**Request → account/equipment context → estimate/approval where needed → Coordinator Assist → Compass schedule → technician cached field work → structured completion/photos/measurements/parts/recommendations → durable submission → coordinator exception review → permanent history/readiness → future technician finds the prior knowledge.**

The Grooming organization must simultaneously feel purpose-built and contain none of the automotive surface.


## Wrapped Tooling Contract
ServiceSlate includes a role/profile-filtered Toolbox rather than scattering helper logic across screens. v0.9 ships 29 wrappers covering deterministic unit/shop/office calculations, planning math, browser/device capabilities, standards-based ICS/vCard interchange, maps/email/SMS/phone handoffs, CSV interoperability, QuickBooks-neutral handoff export, and printable work-order/equipment history. External handoffs never imply delivery or completion. Browser-only tools must detect capability and fail plainly when the browser cannot provide it. Tool-usage logging records the tool identifier and user/time, not calculator inputs.


## Deterministic Assistance & Human Authority

- Deterministic rules and structured data are the default for all core operations.
- AI is disabled by default and is not required for any correct ServiceSlate workflow.
- AI may only be introduced later as an optional ambiguity-resolution assistant; it is never final authority.
- Software-derived consequential information is marked as awaiting human review until a named user accepts/rejects it.
- Human-authored entries already have an accountable author and do not require a redundant second click unless the workflow separately requires coordinator/manager approval.
- Imports/extractions preserve provenance: source, derivation method, review state, reviewer, and review time.
- FastField/Outlook intake priority is machine-readable attachment → deterministic mapping → deterministic matching → document/OCR fallback → optional future AI fallback.
- Safety findings, inspection results, estimates, customer commitments, schedule changes, authorizations, warranty decisions, job completion/cancellation, and permanent-history corrections cannot be silently authored by AI.

## v0.9 communication, integration and production-path amendments

- Outlook is the default office email experience. Technical mail protocols/providers remain implementation details unless Advanced is intentionally opened.
- FastField arriving through Outlook is an intake source, not an authority. ServiceSlate preserves the source, extracts deterministically where possible, and requires a named human to accept consequential imported information.
- Customer awareness of a scheduled service day requires explicit acknowledgement. Sent/opened/read-receipt evidence never substitutes for confirmation.
- In local-only deployment, deterministic email replies may establish explicit customer acknowledgement; ambiguous replies require staff review.
- Customer schedule wording promises the service day unless a human deliberately creates a narrower arrival-window/exact-time commitment.
- Tutorials must be just-in-time and optional. The application should teach through clear screens first; help is a fallback, not a prerequisite.
- Everyday connection setup must speak in outcomes (Outlook Email, Text Messages, Company Files, FastField Intake). Open-source/self-hosted/paid implementation choices belong under Advanced.


## v0.9 production-path contract

- Local-first/no-server use remains a valid deployment. Shared hosting is an optional escalation when live remote synchronization is required.
- Google Drive is a document/file/off-site-backup destination, never the live ServiceSlate relational database. Drive for Desktop folder mode is the lowest-friction company path; direct Shared Drive OAuth is optional.
- PostgreSQL is the hosted relational database. Migration from SQLite must preserve permanent ServiceSlate IDs, audit history and workflow semantics.
- Hosted defaults favor open-source PostgreSQL, Caddy, S3-compatible object storage and ClamAV. External managed services may replace those adapters without changing domain behavior.
- Microsoft Graph is the New Outlook/hosted Microsoft 365 path for mail/calendar. External calendar edits are conflicts requiring normal ServiceSlate validation/human review, not silent schedule truth.
- Company Microsoft sign-in may inherit Entra MFA/passkey policy, but only already-authorized ServiceSlate users may sign in; no auto-provisioning.
- Public customer links require a real HTTPS host. Email-reply and phone/in-person acknowledgement remain valid alternatives.
- Automated SMS is optional. Paired-phone/carrier handoff remains the free/low-friction default; provider delivery states must come from provider evidence.
- QuickBooks live handoff is human-triggered. QBO OAuth and authorized Desktop Web Connector may create accounting records; export-only remains available when permissions are unavailable. ServiceSlate never bypasses hosted QuickBooks administration.
- Public enrichment only creates suggestions. Candidate business facts require user review before saving.
- Automotive Sales & Install preserves Opportunity → Quote → Order → Installation → Commissioning → permanent Equipment/Lifetime Service continuity.
- Production security controls activate with production hosting and must preserve the human-authority, profile-isolation, customer-commitment and no-tracking rules.
- Windows release automation may build/sign/install/update application code, but company data lives separately. Rollback of application code never silently rolls back company data.
- Legal review, zero-training usability tests and real device tests are external human evidence. ServiceSlate may record evidence but cannot mark itself compliant by software assertion.
- A native mobile client is conditional: build it only after real field testing proves a browser/PWA limitation worth the extra implementation/maintenance burden.
