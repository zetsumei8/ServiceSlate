# ServiceSlate Office Network

Office Network mode lets multiple computers on one trusted private Ethernet/Wi-Fi network use the same ServiceSlate data without needing public internet hosting.

## Architecture

There is one authoritative **Office Host**. It owns the ServiceSlate database and attachment store. Joined workstations run the normal ServiceSlate launcher but open the host instead of starting another company database.

This deliberately avoids multi-master SQLite replication. Two independent databases are never merged in the background.

Shared through the Office Host:

- customers, contacts and locations;
- equipment and permanent service history;
- jobs/work orders and statuses;
- Compass/calendar scheduling;
- technician submissions and coordinator review;
- estimates, recommendations, recurring service and follow-ups;
- parts/vehicle stock records;
- files and photos;
- Sales & Install lifecycle records;
- organization configuration and users;
- internal ServiceSlate messages.

Browser-local technician drafts/outbox remain local until acknowledged by the host, preserving the existing offline/reconnect model.

## Set up the host

1. Install ServiceSlate on the office computer that will remain available.
2. Open **ServiceSlate - Configure Office Network** from the Start menu (or run `CONFIGURE_OFFICE_NETWORK.cmd` from a source checkout).
3. Choose **This computer is the Office Host**.
4. Restart ServiceSlate.
5. Open **Office Network** in ServiceSlate. It shows the preferred hostname URL and an IP-address fallback.
6. If Windows Firewall prompts for network access, allow it for **Private networks only**.

Use a reliable office machine and preferably reserve its DHCP address in the router if hostname resolution is unreliable.

## Join another workstation

1. Install the same ServiceSlate release.
2. Open **ServiceSlate - Configure Office Network**.
3. Choose **Join an existing Office Host**.
4. Enter the host address shown by the Office Network screen.
5. Start ServiceSlate normally and sign in with that employee's ServiceSlate account.

A joined workstation will not silently fall back to its own database if the host is unreachable. This prevents split-brain company history.

## Internal communication

The Office Network screen provides:

- active/recent staff presence;
- direct teammate messages;
- office-wide announcements;
- unread state;
- optional record-link metadata at the API level.

Messages are organization-scoped and normal ServiceSlate authentication is required.

## Security boundary

Office LAN mode is for a trusted private network. It does not replace the hardened HTTPS internet deployment. Do not expose TCP port 8765 directly to the public internet or configure router port-forwarding to it.

For remote access across locations or outside the office, use the production HTTPS/PostgreSQL deployment or an organization-approved private VPN rather than exposing LAN mode publicly.
