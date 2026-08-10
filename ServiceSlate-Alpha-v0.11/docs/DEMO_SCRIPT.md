# ServiceSlate Alpha v0.9 — Manager Demo

Demo password for all seeded accounts:

```text
ServiceSlateDemo!26
```

## 1. Start with the coordinator

Sign in as:

`coordinator@demo.example.com`

Show **Coordinator Assist** first. Explain that these are not manually maintained reminder cards: they are derived from actual job, parts, recurring, follow-up, and paperwork states.

## 2. Open a real customer workspace

Open **Future Ford of Clovis**.

Show:
- contacts and approval authority
- locations/bay/equipment
- recent work
- recurring obligations
- estimates
- communication/follow-up history

Use **Edit Account** to demonstrate that corrections update the existing record and retain audit history rather than making a duplicate.

Use **Log Contact** to record a phone call and optionally create a follow-up in the same action.

## 3. Show operational memory

Open the Challenger lift/equipment history or search for a known model/problem.

Point out that prior approved repairs, measurements, inspections, recommendations, and source-imported legacy history stay attached to the equipment and can be found later by another technician.

## 4. Create work with smart defaults

Choose **New Service Request**.

Select a configured service. Show how ServiceSlate suggests duration, crew count, qualification, and work type, while leaving the coordinator free to correct them.

Create the work. Retrying the same command is designed not to create a duplicate job.

## 5. Use Compass and the calendar

Open **Calendar / Compass**.

Show:
- qualification/capacity reasoning
- geographic affinity without GPS tracking
- customer commitment handling
- two-tech requirement
- partial helper overlap where applicable
- nearby-work duplication warning

Explain that Compass recommends; a human approves the schedule.

## 6. Show an inspection

Open **Inspections**.

Create or open a structured lift inspection and show measurements/items/deficiencies/next-due behavior. A failed item requires a deficiency explanation; completed inspections become locked history.

## 7. Switch to the technician

Sign in as:

`tech.chris@demo.example.com`

Open **Technician Companion** and an assigned job.

Show the field context pack:
- customer/site/equipment
- approved historical work
- similar prior repairs
- open recommendations
- warranty/inspection context
- site contacts/access
- files/templates

Enter a field draft and show the device save/sync indicator. Explain that IndexedDB/outbox storage protects work from a browser close or temporary loss of connectivity.

Show structured completion fields: complaint, finding, cause, correction, verification, internal/customer notes, measurements, parts, recommendations, files/photos, safety, labor, signature/outcome.

Submit for coordinator review.

## 8. Coordinator review instead of re-entry

Return to the coordinator account and open **Work Review**.

Show that clean work can be approved quickly and inconsistent/missing work is surfaced as an exception. The coordinator does not retype the technician's paperwork.

Approval updates permanent operational history/readiness.

## 9. Parts and return work

Open **Parts** or a parts-blocked job.

Advance required parts through receiving/readiness states. When all required parts are ready, demonstrate that ServiceSlate makes the blocked return work actionable again instead of relying on someone's memory.

## 10. Estimate + customer decision

Open **Estimates**.

Show lines/revision/assumptions/exclusions. Generate **Customer Link** for an active exact revision.

Open the secure link and approve/decline without creating a customer account. Explain the Alpha limitation honestly: the link is only reachable while the local ServiceSlate origin is reachable; hosted deployment later makes this practical remotely.

## 11. Recurring/recommendations/follow-ups

Show that:
- annual/periodic plans create obligations
- deferred recommendations remain remembered
- follow-ups can be completed/snoozed
- declined work remains history rather than disappearing

## 12. Vehicles without surveillance

Open **Vehicles**.

Show branch/technician assignment, availability, soft truck inventory, and an approved remote-start concept.

Explicitly show what is absent: no GPS, dash cam, speed, route history, geofence, or driver scoring.

## 13. Backup, recovery, export, migration

Open **Health & Backup**.

Show:
- schema/system health
- local storage status
- create backup
- verify backup
- download/restore controls
- full export
- customer CSV import
- FastField/NoteWise work-history staging/import

Explain that ambiguous legacy records are held for review instead of guessed.

## 14. Show the Grooming profile

Sign in as:

`reception@demo.example.com`

Show that the navigation is purpose-built for grooming and contains no automotive equipment/parts/vehicles.

Demonstrate:
- pet profile/safety context
- configurable grooming service
- appointment creation
- forms
- waitlist
- cancellation surfacing eligible waitlist matches
- completed appointment creating a rebooking obligation

## 15. Optional real-business setup proof

Sign out and choose **Set Up My Business**.

Show the guided profile/business/admin setup. The user does not configure a database, server, ports, Docker, or environment variables.

## Customer service-day confirmation

From a scheduled automotive work order, open **Customer Confirmation**. ServiceSlate presents the scheduled day as the customer commitment while keeping the internal planning start private. Generate the secure confirmation link and either copy it, hand it to email/SMS, or record a phone/in-person confirmation.

On the public link, confirm the service day. Back in ServiceSlate, the work order shows **Customer Confirmed**. Reschedule the work order and show that the old acknowledgement becomes **Date Changed** rather than silently carrying forward.

## Outlook / FastField / customer-awareness demo

1. Sign in as coordinator.
2. Open **Tools & Connections**. Point out that the normal choices are Outlook, paired-phone texts, Company Files, and FastField Intake; technical connectors are hidden under Advanced.
3. Choose **Check for New Mail**. On a non-Windows demo machine, use **`demo_samples/FastField-Sample.eml`** with the saved-email fallback to stage a sample FastField report.
4. Show the FastField batch as **Needs human approval** and the provenance note **AI not used**.
5. Approve a clean row and show the resulting history in the equipment service story.
6. Open a scheduled job and choose Customer Confirmation.
7. Show the prepared Outlook email. Emphasize the service **day**, not a promised internal arrival hour.
8. Import a customer reply email containing `CONFIRM` and the ServiceSlate reference. Refresh the job and show explicit customer acknowledgement.
9. Explain that a read receipt is supporting evidence only; ambiguous replies do not confirm the appointment.
10. Open **? Help** and show one three-step guide, then exit it. Explain that it appears once and stays out of the way afterward.


## v0.9 real-company path (optional)

After the core manager demo, open **Production Readiness** to show that ServiceSlate distinguishes code that exists from outside activation that still needs the company.

Show, without claiming they are already authorized:
- Company Drive: existing Drive for Desktop folder can mirror documents/backups; direct Shared Drive is optional.
- Microsoft 365: New Outlook mail/calendar/company sign-in connection is ready for company app approval.
- QuickBooks: export always works; QBO and authorized Desktop Web Connector live paths are available.
- Sales & Install: opportunity → quote → order → installation → installed equipment → commissioning.
- Hosting: PostgreSQL/HTTPS/object-storage deployment is packaged for when remote live sync is wanted.
- Human evidence: legal wording, zero-training and real-device readiness remain human tasks, not software self-certification.
