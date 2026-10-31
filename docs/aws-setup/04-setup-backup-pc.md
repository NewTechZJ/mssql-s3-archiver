# Step 4 – Set up the backup PC (`aws configure`)

[← Step 3 – IAM user](03-create-iam-user.md) · [README](../../README.md) · Next: [Step 5 – CloudTrail →](05-setup-cloudtrail.md)

Now you'll save the `sql-uploader` access key on the PC that runs the backups.

**Time:** about 10 minutes
**You need:** the `.csv` key file from Step 3 and your Region code

---

## 1. Sign in as the right Windows user

> ⚠️ **Important:** AWS saves the key **for the Windows user who runs `aws configure`**.
> Sign in to the PC as the **same Windows account that will run the backup** (the one you'll use in Task Scheduler).
> If you set it up as one user and the backup runs as another, it fails with *"Unable to locate credentials"*.

## 2. Install the AWS CLI

1. Download the installer: <https://awscli.amazonaws.com/AWSCLIV2.msi>
2. Double-click it and click **Next** until it's done.
3. **Close and reopen** any Command Prompt windows.

## 3. Save the key

1. Click **Start**, type `cmd`, and open **Command Prompt**.
2. Type this and press **Enter**:

```
aws configure
```

3. It asks four questions. Answer them from your `.csv` file and notes:

| Question | What to type |
|---|---|
| `AWS Access Key ID` | The **Access key ID** from the .csv (starts with `AKIA`) |
| `AWS Secret Access Key` | The **Secret access key** from the .csv |
| `Default region name` | Your Region code, for example `us-east-1` |
| `Default output format` | `json` |

> 💡 To paste in Command Prompt, **right-click**. Nothing appears on screen while you type the secret key. That's normal.

## 4. Test the connection

Type each line and press **Enter**:

```
aws sts get-caller-identity
```
✅ The output should contain `user/sql-uploader`.

```
aws s3 ls s3://YOUR-BUCKET-NAME
```
✅ No error. A new bucket shows nothing, which is fine.

```
aws sns publish --topic-arn YOUR-SNS-TOPIC-ARN --subject "Test" --message "Hello from the backup PC"
```
✅ You get the email.

## 5. Tell the backup tool where to send backups

Open `config.json` next to `SqlBackupUploader.exe` with Notepad and set:

```json
"bucket_name": "YOUR-BUCKET-NAME",
"aws_region": "us-east-1",
"aws_profile": "",
"sns_topic_arn": "YOUR-SNS-TOPIC-ARN",
"manage_lifecycle": true
```

Leave `"aws_profile": ""` empty. The tool then uses the key you just saved.

## 6. Do a safe test run

1. Run **`SqlBackupUploader_TEST.exe`**. It uses dummy data and doesn't touch SQL Server.
2. Click **Start Test Run**.
3. ✅ Every step turns green, you get a "SQL Backup OK" email, and a dated folder appears in your S3 bucket.

## Common problems

| Message | Fix |
|---|---|
| `'aws' is not recognized` | Close and reopen Command Prompt, or restart the PC after installing. |
| `Unable to locate credentials` | You ran `aws configure` as a different Windows user. Repeat it as the backup user. |
| `AccessDenied` | Check that the bucket name and topic ARN in the Step 3 policy are spelled correctly. |
| `InvalidClientTokenId` / `SignatureDoesNotMatch` | The key was typed or pasted wrong. Run `aws configure` again. |
| Clock-related errors | Fix the PC's date, time and time zone. |
