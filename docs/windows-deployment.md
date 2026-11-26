# Windows LAN deployment

[简体中文](windows-deployment.zh-CN.md)

Install the platform on one always-on x64 Windows host. Teammates access the
host's Web URL. The host runs one Web service, one Huey worker for both queues,
and optionally one collaboration process. PostgreSQL can run locally or on an
internal database server.

## Build and distribute a release

The **Windows release** GitHub Actions workflow runs the existing PostgreSQL
and frontend CI, builds the static assets and packages the application with
Windows Python wheels. Run it manually on a branch for a preview artifact.
Push a `vMAJOR.MINOR.PATCH` tag (an optional prerelease suffix is supported)
for a GitHub Release containing the ZIP and its `.sha256` file. Copy both to the
target host or its internal distribution share. The package contains runtime
files and Python dependencies. Install Python, PostgreSQL, WinSW and Synopsys
Silver separately, using your organization's approved installers.

## Install a new host

1. Install system-wide **x64 CPython 3.10.18**, PostgreSQL and its client tools,
   and [WinSW 2.x](https://github.com/winsw/winsw/releases/tag/v2.12.0).
   PostgreSQL 16 matches CI. Provision a dedicated empty PostgreSQL database
   owned by the application role and put `pg_dump.exe` and `pg_restore.exe` on
   the administrator's PATH. For real execution, install Silver and configure
   the license on this host.

   To install the validated Python patch release using an approved copy of
   [uv](https://docs.astral.sh/uv/guides/install-python/), run
   `uv python install 3.10.18 --install-dir C:\ProgramData\SilverPython --no-bin`.
   Use the resulting `python.exe` path for `-Python` and allow the Windows
   service account to read that installation.
2. Verify the ZIP checksum against the SHA-256 value from the trusted release
   before extracting or running its script. Use an elevated Windows PowerShell
   prompt and extract the ZIP to a separate temporary directory.

   ```powershell
   $package = 'C:\Install\silver-test-platform-v2.13.0-windows-x64.zip'
   (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash
   Get-Content -LiteralPath "$package.sha256"
   Expand-Archive -LiteralPath $package -DestinationPath 'C:\Install\silver-setup'
   Set-Location 'C:\Install\silver-setup'
   .\deployment\Manage-Silver.ps1 Initialize -WinSWPath 'C:\Install\WinSW.NET461.exe'
   ```

3. Edit `C:\ProgramData\SilverTestPlatform\config\.env`. Set `DATABASE_URL`, a
   unique `SECRET_KEY` of at least 32 characters and `LM_ADMIN_PASSWORD`. Use the
   same database for the platform and Huey. Use `RUNNER_BACKEND=mock` for an
   initial smoke test, or `RUNNER_BACKEND=silver` with the target `SILVER_HOME` for
   real tests. The script assigns the storage and log paths below. Allow the
   appropriate inbound Web port (default `8080`) for the intended LAN clients.
4. Install the release. Add `-Collab` on first install if the host should also
   run collaboration. Configure the browser WebSocket URL or use the reverse
   proxy setup in the [main README](../README.md#real-time-collaboration-optional-third-process).

   ```powershell
   .\deployment\Manage-Silver.ps1 Install -Package $package -Python 'C:\Program Files\Python310\python.exe'
   .\deployment\Manage-Silver.ps1 Status
   ```

5. Open the Web URL from a client PC, sign in, and run a representative test.
   On the real Silver host, do this with the service account and without an
   interactive session. WinSW defaults to `NT AUTHORITY\LocalService`. Give
   that account access to the installed Python and Silver runtime, any external
   model inputs, and the configured license. If your site uses a dedicated
   service account, change the service's Log On account in Windows Services and
   grant it the corresponding access. The installer only sets the account on
   new services.

The default deployment root uses `config\.env` for credentials, `releases\`
for versioned code and virtual environments, `data\` for durable files,
`logs\` for rotating logs and `backups\` for upgrade snapshots. The three data
roots are `data\instance`, `data\uploads` and `data\reports`. Keep the root
on a local NTFS volume. For a custom root specify `-Root` on each invocation.
Keep an administrator-only backup copy on a separate disk or internal server.

### Move an existing installation

Schedule a maintenance window on the source host, finish jobs and stop all
writers. Back up its application database and copy its instance, upload and
report directories, including model versions and workspaces. Restore into a
fresh database on the destination and copy the files into the corresponding
managed data directories before the first `Install`. Some existing records
store absolute paths. Preserve their paths where possible or validate and
re-register affected models and inputs before cutover. Check representative
historical reports and model snapshots as well as a new test run. Keep the
source stopped during cutover if the two hosts use the same database.

## Upgrade or resume

Schedule a window and ask users to finish submissions and close live editors.
Wait for Silver runs, unit-test batches, AI generation and import/export jobs
to finish. Start the deployment script from the verified new release.

```powershell
.\deployment\Manage-Silver.ps1 Update -Package $package -Python 'C:\Program Files\Python310\python.exe'
.\deployment\Manage-Silver.ps1 Status
```

The script stages the environment before stopping services. It checks for
active work again after stopping Web and collaboration, then stops the worker.
It saves the pre-upgrade PostgreSQL dump, durable files and `.env` and prints
the snapshot directory. It runs `manage.py bootstrap` once before starting the
new services and checking the Web version, database and optional collaboration
endpoint. Finish the release check with a real Silver smoke test when using
that backend. Use `Stop` and `Start` to take the active version down and bring
it back without running bootstrap.

If deployment stops after the maintenance snapshot, `Status` shows the
incomplete release. Fix the failure and run `Resume` to retry activation.
Keep the installed releases and their snapshots for as long as you need to be
able to recover them. The service logs are in `logs\`.

## Roll back to a pre-upgrade snapshot

1. If the new release is running, finish jobs and run `Stop`. Confirm all
   platform services are stopped. Keep the database of the new release and
   preserve any data written since the snapshot for later reconciliation.
2. Create a **new, empty PostgreSQL database** owned by the application role.
   Recover the snapshot's configuration from `backups\<snapshot>\.env` into
   `config\.env` and point `DATABASE_URL` at the new database. If a
   `HUEY_DATABASE_URL` override is present, remove it or make it identical.
3. Run rollback with the snapshot directory name from `Status` or `backups\`.

   ```powershell
   .\deployment\Manage-Silver.ps1 Rollback -Backup '20261126-123456-a1b2c3d4'
   .\deployment\Manage-Silver.ps1 Status
   ```

The script restores the dump in one transaction, restores the corresponding
durable files, preserves the newer files under `preserved-data-*`, and starts
the release recorded in the snapshot. If the restore was interrupted, retry
with a fresh empty database. If only startup failed after the restore, fix that
failure and use `Resume`. Recheck the application and a representative job
before reopening to users.
