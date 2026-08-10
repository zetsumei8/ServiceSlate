# ServiceSlate Windows Release Channel

`windows-release.yml` builds a standalone PyInstaller application and NSIS Setup EXE on a real Windows GitHub runner.

If repository secrets `WINDOWS_SIGNING_PFX` and `WINDOWS_SIGNING_PASSWORD` are configured, the application EXE and Setup EXE are Authenticode signed and timestamped. Without a company certificate the workflow can still prove the build, but the artifact must not be presented as a professionally signed production release.

The generated `update-manifest.json` contains the release version, SHA-256 and installer filename. ServiceSlate resolves a relative installer next to the HTTPS manifest, verifies SHA-256, and in normal production requires a valid Windows signature before launching it.

For one-click **application rollback**, a release manifest may also include `rollback_url` and `rollback_sha256` for the verified previous-version installer. ServiceSlate preserves that package during update and can later launch it after creating another safety backup.

Application rollback is deliberately separate from database restore. Company data lives outside the application install directory, and ServiceSlate never silently restores an older database merely because application code was rolled back.
