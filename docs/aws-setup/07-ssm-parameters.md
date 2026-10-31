# Step 7 (optional) – Keep settings in AWS SSM Parameter Store

[← Step 6 – Budget](06-create-budget.md) · [README](../../README.md)

Instead of typing every setting into `config.json` on the backup PC, you can store settings in AWS **SSM Parameter Store**. The program reads them each time it starts.

**Why use it?**
- You can change settings (bucket, retention, alert topic...) in AWS **without going to the backup PC**.
- Several servers can share the same settings.
- The SQL password can be stored **encrypted** (SecureString) instead of in a text file.

**Time:** about 15 minutes
**Cost:** free (Standard parameters)

---

## How the program decides which value to use

For every setting, the program checks these places **in this order** and uses the first one it finds:

| Order | Place | Notes |
|---|---|---|
| 1 | `config.json` next to the program | Wins if the key is in the file **with a real value** |
| 2 | SSM Parameter Store | Read from the path in `config_path` |
| 3 | Built-in default | |

> ⚠️ **Most common mistake:** a key in `config.json` with an **empty value `""` still wins** over SSM.
> For any setting you want to come from SSM, either **delete that line** from `config.json` or leave it as the `<optional: ...>` / `<replace: ...>` text from the example.

---

## 1. Choose a path

All parameters live under one "folder" name, called a **path**. Example: `/sqlbackup`

> ✏️ **Write down** your path. Use a different path for each server or database if they need different settings, for example `/sqlbackup/server01`.

## 2. Create the parameters

1. Sign in to the AWS console as an administrator. Check that the **Region** (top right) is the **same Region as your bucket**.
2. In the search box, type **Parameter Store** and click it (it's part of *Systems Manager*).
3. Click **Create parameter**.
4. Fill in:

| Field | What to type |
|---|---|
| **Name** | Your path + `/` + the **setting name exactly as in `config.json`**. Example: `/sqlbackup/bucket_name` |
| **Tier** | **Standard** |
| **Type** | **String** (use **SecureString** for `sql_password`) |
| **KMS key source** (SecureString only) | **My current account**, key `alias/aws/ssm` |
| **Value** | The value only, with **no quotes**. Example: `acme-sql-backups` |

5. Click **Create parameter**.
6. Repeat for each setting you want in SSM.

### Example set of parameters

| Name | Type | Value |
|---|---|---|
| `/sqlbackup/database` | String | `SalesDB` |
| `/sqlbackup/bucket_name` | String | `acme-sql-backups` |
| `/sqlbackup/s3_prefix` | String | `prod` |
| `/sqlbackup/sns_topic_arn` | String | `arn:aws:sns:us-east-1:123456789012:sql-backup-alerts` |
| `/sqlbackup/cheap_tier_retention_days` | String | `60` |
| `/sqlbackup/sql_username` | String | `backup_user` |
| `/sqlbackup/sql_password` | **SecureString** | *(the password)* |

### Rules for names and values

- The last part of the name must match a setting name **exactly**, in lowercase (`bucket_name`, not `BucketName`). A misspelled name is **ignored**, and the run log shows a warning.
- Put parameters **directly** under your path. `/sqlbackup/bucket_name` works; `/sqlbackup/extra/bucket_name` is not read.
- **Numbers:** type just the number, like `30`.
- **Yes/No settings** (like `manage_lifecycle`): type `true` or `false`.
- If a number setting has a non-number value, the backup **stops with an error**. This is on purpose, so a typo can't silently change your backups.
- `cheap_tier_retention_days` can never go below **30**, wherever it is set.

### Settings that can NOT come from SSM

These four are always read from `config.json` only, because the program needs them before it can reach SSM:

`aws_region` · `aws_profile` · `config_path` · `start_mode`

## 3. Allow `sql-uploader` to read the parameters

1. Open **IAM** → **Policies** → `SqlBackupUploaderPolicy` → **Edit** → **JSON**.
2. Add this block inside the `"Statement": [ ... ]` list. Put a comma after the block before it.
3. Replace `us-east-1`, `123456789012` (your account ID) and `sqlbackup` (your path).

```json
    {
      "Sid": "ReadSettings",
      "Effect": "Allow",
      "Action": "ssm:GetParametersByPath",
      "Resource": [
        "arn:aws:ssm:us-east-1:123456789012:parameter/sqlbackup",
        "arn:aws:ssm:us-east-1:123456789012:parameter/sqlbackup/*"
      ]
    }
```

4. Click **Next**, then **Save changes**.

> ℹ️ You don't need a KMS permission when the SecureString uses the default key `alias/aws/ssm`. If you chose your own KMS key, also allow `kms:Decrypt` on that key.

> 💡 Your account ID is the 12-digit number in the top-right menu of the AWS console.

## 4. Point `config.json` at SSM

On the backup PC, edit `config.json`. Keep only the local settings, and delete (or leave as `<...>` text) everything that now comes from SSM:

```json
{
    "aws_region": "us-east-1",
    "config_path": "/sqlbackup",
    "backup_folder": "D:\\SqlBackups"
}
```

Settings that are only in this file (like `backup_folder` above) still work. Anything not set anywhere uses the built-in default.

## ✅ Check it worked

1. Run the **TEST** program (it also reads SSM). If you use the TEST program, put `"config_path"` in `config.test.json` too.
2. Look at the top of the Activity log, or the run log in the `Logs` folder:

```
Configuration source: cloud  (file: C:\SqlBackup\config.json)
  bucket_name = 'acme-sql-backups'  (cloud)
  s3_prefix = 'prod'  (cloud)
  sns_topic_arn = ''  (local)        ← "local" means config.json won; delete the line there
```

- **`(cloud)`** = the value came from SSM ✅
- **`(local)`** = the value came from `config.json`
- **`(default)`** = not set anywhere, built-in default used

## What happens if SSM can't be reached

After every successful read, the program saves a copy of the SSM values in `Logs\config_cache.json`. If AWS can't be reached next time (for example, a network problem), it uses that copy and writes a warning in the log. If there is no copy yet, the run stops with an error.

> ⚠️ **Security note:** this cache file stores values **in plain text, including a SecureString `sql_password`**. Make sure only administrators and the backup account can open the program's `Logs` folder. If that isn't possible, keep the password in the `SQLBACKUP_SQL_PASSWORD` Windows environment variable instead of SSM.

## Common problems

| What you see | Fix |
|---|---|
| `Cannot read cloud configuration at '/sqlbackup': ... AccessDenied` | Do section 3 again. Check the Region, account ID and path in the policy. |
| `No usable parameters found under /sqlbackup` | The parameters are in a different Region from `aws_region`, or the path is spelled differently. |
| `SSM parameter 'xyz' ignored (unknown setting...)` | Fix the spelling of the parameter name. |
| `SSM parameter 'aws_region' ignored (...config.json only)` | Delete that parameter. Set it in `config.json`. |
| A setting shows `(local)` but you wanted SSM | Delete that line from `config.json`. |
| `SSM parameter 'part_count' has an invalid value` | Change the value to a plain number. |
