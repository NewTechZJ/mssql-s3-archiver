# Step 3 – Create the `sql-uploader` user

[← Step 2 – SNS topic](02-create-sns-topic.md) · [README](../../README.md) · Next: [Step 4 – Backup PC →](04-setup-backup-pc.md)

The backup program needs its own AWS login. This user:
- **cannot** sign in to the AWS website (command line access only), and
- **can only** work with your backup bucket and send alerts.

If someone ever steals its key, the damage is limited.

**Time:** about 10 minutes
**You need:** the bucket name (Step 1) and the SNS topic ARN (Step 2)

---

## 1. Create the permission policy

A **policy** is a list of what the user is allowed to do.

1. In the search box at the top, type **IAM** and click it.
2. In the left menu, click **Policies**, then **Create policy**.
3. Click the **JSON** button (next to *Visual*).
4. Delete all the text in the box and paste the text below.
5. Change these three parts:
   - `YOUR-BUCKET-NAME` → your bucket name (**it appears 2 times**)
   - `YOUR-SNS-TOPIC-ARN` → your full topic ARN

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BackupBucket",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketVersioning",
        "s3:GetLifecycleConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:ListBucketMultipartUploads"
      ],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME"
    },
    {
      "Sid": "BackupFiles",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:GetObjectTagging",
        "s3:PutObjectTagging",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*"
    },
    {
      "Sid": "ReplaceManifestOnly",
      "Effect": "Allow",
      "Action": "s3:DeleteObject",
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*manifest.json"
    },
    {
      "Sid": "Notify",
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "YOUR-SNS-TOPIC-ARN"
    }
  ]
}
```

6. Click **Next**.
7. **Policy name:** `SqlBackupUploaderPolicy`
8. Click **Create policy**.

> ℹ️ There is no "delete" permission here on purpose. Old backups are removed automatically by S3 rules, not by this user. If the PC is ever hacked, the attacker can't delete your backups.

## 2. Create the user

1. In the left menu, click **Users**, then **Create user**.
2. **User name:** `sql-uploader`
3. **Do NOT** tick *Provide user access to the AWS Management Console*.
4. Click **Next**.
5. Choose **Attach policies directly**.
6. In the search box, type `SqlBackupUploaderPolicy` and ✅ tick it.
7. Click **Next**, then **Create user**.

## 3. Create the access key

The access key is like a username and password for the program.

1. Click the user name **sql-uploader**.
2. Open the **Security credentials** tab.
3. Scroll to **Access keys** and click **Create access key**.
4. Choose **Command Line Interface (CLI)**.
5. Tick the confirmation box at the bottom, then click **Next**.
6. **Description tag:** the name of the backup PC (for example `SQLSERVER01`).
7. Click **Create access key**.
8. Click **Download .csv file** and keep it somewhere safe.

> ⚠️ **This is the only time AWS shows the secret access key.** If you lose it, delete the key and create a new one.
> Never email it, put it in chat, or save it in the GitHub project.

## ✅ Check it worked

The **Users** list shows **sql-uploader** with:
- **Console access:** *Disabled*
- **Access key:** *Active*
