# ServiceSlate + Outlook / Microsoft 365

## Local/lowest-friction
Classic Outlook on Windows can use the already signed-in desktop Outlook profile while ServiceSlate is open.

## New Outlook / hosted use
Microsoft Graph is the supported ServiceSlate path for:
- mailbox intake;
- FastField submissions arriving by email;
- customer confirmation replies;
- outbound mail;
- two-way calendar synchronization;
- optional company Microsoft sign-in.

The company must approve/register the Microsoft application. ServiceSlate does not scrape Outlook web pages or invent tenant credentials.

## Calendar authority
ServiceSlate is the operational schedule authority. External Outlook edits/deletes become reviewable conflicts. Outlook cannot silently cancel or materially move a ServiceSlate visit.

## Identity
Only an already-authorized ServiceSlate user can complete company Microsoft sign-in. ServiceSlate does not auto-create accounts from Microsoft. The company's Entra configuration can enforce MFA/passkeys.
