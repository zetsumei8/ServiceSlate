# ServiceSlate + QuickBooks

ServiceSlate remains operational truth. QuickBooks remains accounting truth after an accounting record is accepted there.

## Always available
QuickBooks-neutral billing/export files.

## QuickBooks Online
When authorized by the company:
- OAuth connection;
- customer match/create;
- human-triggered invoice creation from an approved ServiceSlate estimate;
- returned QuickBooks invoice ID stored as integration evidence;
- retry does not create another invoice after a successful link exists.

## QuickBooks Desktop / hosted Desktop
If the QuickBooks administrator allows Intuit Web Connector:
- configure the hosted HTTPS ServiceSlate address, connector username/password and QuickBooks service item name;
- download the generated `.qwc` file;
- a manager/coordinator explicitly queues an approved ServiceSlate estimate for accounting;
- Web Connector authenticates to ServiceSlate and receives QBXML;
- QuickBooks TxnID or rejection/error is reconciled back to the queue/audit trail.

ServiceSlate cannot bypass the hosted QuickBooks administrator or permissions. If Web Connector cannot be authorized, use export-only or another permitted method.
