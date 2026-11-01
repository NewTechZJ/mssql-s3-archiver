# SQL Backup Uploader

[← Back to README](../README.md) · AWS setup guides: [Step 1](aws-setup/01-create-s3-bucket.md)

A Windows tool that backs up a Microsoft SQL Server database, checks the backup, and stores it safely in **Amazon S3**. It manages storage cost and retention for you.

It runs as a small desktop window with a **Start** button. It can also run with no window from Windows Task Scheduler.

```
SQL Server ──► striped FULL backup (N files) ──► RESTORE VERIFYONLY ──► S3 /YYYY-MM-DD/
                                                                          │
                     older dated folders ──► cheap storage class + auto-expiry
```

## Features

- **Striped, compressed FULL backup** split into N files (default 30) with `CHECKSUM`.
- **Verification** of every backup with `RESTORE VERIFYONLY ... WITH CHECKSUM` before upload.
- **Dated S3 folders** (`<prefix>/YYYY-MM-DD/`). Each folder gets a `manifest.json` and that run's log.
- **Resumable uploads.** If a run fails during upload, the next run that day uploads only what is missing. Parts already in S3 with the right size are skipped. Multipart upload is used, with optional bandwidth limit.
- **Safe retention.** Older folders are moved to a cheaper storage class only after the newest backup is complete in S3. A lifecycle rule then deletes them after `cheap_tier_retention_days`. The newest backup is never scheduled for deletion.
- **Local disk housekeeping.** It checks free space before the backup and deletes local sets only after their upload is confirmed in S3. Old run logs are removed too.
- **Configuration** comes from `config.json` and/or **AWS SSM Parameter Store**. There is an offline cache in case SSM can't be reached.
- **Notifications** through **Amazon SNS** on success or failure.
- **Monitoring mode** (`--check`) sends an alert when the newest complete backup is too old.
- **Bucket hardening** (`--setup-bucket`) turns on block public access, SSE-S3 encryption, HTTPS-only access, versioning, an optional Object Lock and lifecycle rules.
- **Test mode** (`--test`) uses dummy data instead of SQL Server, so you can try out the S3 side safely.
- **Run steps one at a time** (backup, verify, upload, archive, cleanup) for debugging.

## Requirements

| Component | Notes |
|---|---|
| Windows | Server or desktop. The GUI and the `.exe` build are Windows-only. |
| Python 3.10+ | Needed to run from source or to build the `.exe`. |
| SQL Server | Must be an edition that supports `BACKUP ... WITH COMPRESSION`. **Express supported, but backups won’t be compressed.** |
| ODBC driver | [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) is recommended. Versions 17/13 also work. |
| AWS account | You need an S3 bucket. SSM and SNS are optional. |

The SQL login, or the Windows account in use, needs the `BACKUP DATABASE` permission. The SQL Server service account needs write access to `backup_folder`.

## Installation

```powershell
git clone https://github.com/<your-user>/sql-backup-uploader.git
cd sql-backup-uploader
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json   # then edit config.json
```

### Building the `.exe`

Run `build.bat` on a Windows PC that has Python 3.10+ installed. It builds two programs in `dist\`:

| Program | Mode | Config file |
|---|---|---|
| `SqlBackupUploader.exe` | Real backups | `config.json` |
| `SqlBackupUploader_TEST.exe` | Dummy 30 KB data, no SQL Server | `config.test.json` |

The mode is chosen by the program's file name: a name that ends in `_TEST` runs in test mode. On a fresh clone, the first run of `build.bat` creates `config.json` and `config.test.json` from the example and stops so you can edit them. Optional window icons (`SqlBackupUploader.ico`, `SqlBackupUploader_TEST.ico`) are bundled automatically if they are in the folder.

## Configuration

Settings are read in this order. The first source that has a value wins:

1. **`config.json`** next to the program. Any key in this file wins, even when it is empty.
2. **AWS SSM Parameter Store**, if `config_path` is set (for example `/sqlbackup`). Use one parameter per setting, such as `/sqlbackup/bucket_name`. `SecureString` works well for `sql_password`.
3. The built-in defaults.

`aws_region`, `aws_profile`, `config_path` and `start_mode` are read only from `config.json`.

### Key settings

| Setting | Default | Description |
|---|---|---|
| `sql_server` | `localhost` | SQL Server instance |
| `database` | *(required)* | Database to back up |
| `sql_username` / `sql_password` | empty | Leave empty to use Windows authentication. The password can also come from the `SQLBACKUP_SQL_PASSWORD` environment variable. |
| `backup_folder` | *(required)* | Local folder for the backup files |
| `part_count` | `30` | Number of stripe files (SQL Server allows at most 64) |
| `bucket_name` | *(required)* | Target S3 bucket |
| `s3_prefix` | empty | Folder inside the bucket. Leave empty to use the bucket root. |
| `upload_storage_class` | `STANDARD` | Storage class used for new uploads |
| `cheap_storage_class` | `STANDARD_IA` | Storage class older folders are moved to |
| `cheap_tier_retention_days` | `60` | Days before archived folders are deleted (at least 30) |
| `manage_lifecycle` | `true` | Set to `false` to leave the bucket's lifecycle rules alone after `--setup-bucket` |
| `local_keep_sets` | `1` | Number of uploaded backup sets to keep on local disk |
| `max_bandwidth_mb_per_s` | `0` | Upload speed limit (`0` = unlimited) |
| `sns_topic_arn` | empty | SNS topic for notifications |
| `max_backup_age_days` | `9` | Age limit used by `--check` |
| `backup_timeout_hours` / `verify_timeout_hours` | `6` | Time limits for each step |

The full list, with comments, is in the `DEFAULTS` block in `sql_backup_uploader.py`.

### AWS credentials and permissions

The standard AWS credential chain is used: environment variables, `~/.aws/credentials`, a named `aws_profile`, or an instance role. [`docs/iam-policy.json`](iam-policy.json) has a least-privilege policy for the backup machine. Replace the bucket name, account ID and ARNs with your own. `--setup-bucket` needs more permissions, so run it once with an administrator profile.

## Usage

```powershell
# One-time bucket hardening (administrator)
python sql_backup_uploader.py --setup-bucket --profile admin
python sql_backup_uploader.py --setup-bucket --profile admin --object-lock-days 30

# Interactive window
python sql_backup_uploader.py

# Scheduled / headless run (exit code 0 = OK, 1 = failed)
python sql_backup_uploader.py --auto

# Freshness check, ideally from a different machine
python sql_backup_uploader.py --check

# Try it safely with dummy data (no SQL Server needed)
python sql_backup_uploader.py --test --config config.test.json
python sql_backup_uploader.py --test --auto --date 2026-01-01   # create an "older" folder

# Run individual steps
python sql_backup_uploader.py --auto --steps 1,2        # backup + verify only
python sql_backup_uploader.py --auto --steps upload     # upload today's existing parts
python sql_backup_uploader.py --auto --steps 3 --date 2026-10-28
```

| Step | Name | What it does |
|---|---|---|
| 1 | `backup` | `BACKUP DATABASE ... WITH INIT, COMPRESSION, CHECKSUM` into N parts |
| 2 | `verify` | `RESTORE VERIFYONLY ... WITH CHECKSUM` |
| 3 | `upload` | Uploads the parts, then `manifest.json`, to `<prefix>/YYYY-MM-DD/` |
| 4 | `archive` | Moves older dated folders to the cheap tier and applies lifecycle rules. It refuses to run unless today's folder is complete. |
| 5 | `cleanup` | Deletes local sets that are safely in S3, and old run logs |

### Scheduling with Task Scheduler

Create a task that runs `SqlBackupUploader.exe --auto`, or `python sql_backup_uploader.py --auto`, under an account that has SQL and AWS access. Choose **"Run whether user is logged on or not"**. Only one instance runs at a time. A second instance exits with code `3`.

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Run failed (or `--check` found the backups overdue) |
| 2 | Configuration error |
| 3 | Another instance is already running |

## S3 layout

```
s3://my-bucket/prod/
├── 2026-10-27/                 ← older: STANDARD_IA, tagged tier=cheap, expires after N days
│   ├── Sales_Full_20261027_Part1.bak ... Part30.bak
│   ├── manifest.json
│   └── run_log_20261027_020013.log
└── 2026-10-28/                 ← newest: never tagged, never auto-deleted
    └── ...
```

A folder without `manifest.json` is an incomplete upload. Don't restore from it.

## Restoring a backup

1. Download every part from the dated folder you want, for example with the AWS CLI:
   ```powershell
   aws s3 cp s3://my-bucket/prod/2026-10-28/ D:\Restore\ --recursive --exclude "*" --include "*.bak"
   ```
   Check the file sizes against `manifest.json`. If the folder is in `GLACIER` or `DEEP_ARCHIVE`, you need to run an S3 *restore* request first.
2. Restore **using all parts at once**:
   ```sql
   RESTORE DATABASE [Sales] FROM
     DISK = N'D:\Restore\Sales_Full_20261028_Part1.bak',
     DISK = N'D:\Restore\Sales_Full_20261028_Part2.bak',
     -- ... every part up to PartN
   WITH CHECKSUM, STATS = 10, RECOVERY;
   -- add MOVE '...' TO '...' clauses when restoring to a different server/path
   ```

Practice a restore regularly. A backup you have never restored is untested.

## Logs

- `Logs\backup_upload.log` is the rolling log for all runs (50 MB × 10 files).
- `Logs\run_log_YYYYMMDD_HHMMSS.log` is the log for one run. A copy is also uploaded to that run's S3 folder.
- `Logs\config_cache.json` is the last SSM configuration that was read successfully.

## Known limitations / roadmap

- [ ] **Secrets on disk.** `Logs\config_cache.json` stores SSM values in plain text, and that includes a decrypted `sql_password`.
- [ ] `part_count` is not checked against SQL Server's limit of 64 devices.
- [ ] `manifest.json` records sizes but no checksums.
- [ ] The connection uses `TrustServerCertificate=yes`, which skips certificate validation. That is fine for `localhost` but not ideal for remote servers.
- [ ] Only full backups. There are no differential or transaction-log backups.
- [ ] There is no built-in restore command and no automated tests.

## License

[MIT](../LICENSE)
