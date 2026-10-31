# Step 5 – Turn on CloudTrail (activity record)

[← Step 4 – Backup PC](04-setup-backup-pc.md) · [README](../../README.md) · Next: [Step 6 – Budget →](06-create-budget.md)

**CloudTrail** keeps a record of every change made in your AWS account: who did it, what they did, and when. If something goes wrong (for example, a bucket setting changes), you can find out how.

AWS already keeps **90 days** of history for free. A **trail** saves the history for as long as you want.

**Time:** about 5 minutes
**Cost:** the first trail is free for normal account activity. You only pay a small amount for storing its log files in S3.

---

## 1. Create the trail

1. In the search box at the top, type **CloudTrail** and click it.
2. In the left menu, click **Trails**, then **Create trail**.
3. Fill in:

| Field | What to choose |
|---|---|
| **Trail name** | `account-activity` |
| **Storage location** | **Create new S3 bucket** (let AWS name it) |
| **Log file SSE-KMS encryption** | **Untick** it. *(If you leave it ticked, it adds a KMS key at about $1/month. Your logs are still encrypted either way.)* |
| **Log file validation** | ✅ **Enabled** (shows if anyone edits the logs) |
| **CloudWatch Logs** | Leave **off** |

4. Click **Next**.

## 2. Choose what to record

| Field | What to choose |
|---|---|
| **Management events** | ✅ Ticked, with **Read** and **Write** both ticked |
| **Data events** | ❌ **Leave unticked** |
| **Insights events** | ❌ Leave unticked |
| **Network activity events** | ❌ Leave unticked |

> ⚠️ **Don't turn on Data events for the backup bucket.** That would record every file upload and can cost more than the backups themselves.

5. Click **Next**, then **Create trail**.

## ✅ Check it worked

- The **Trails** list shows `account-activity` with **Status: Logging**.
- After about 15 minutes, the new CloudTrail S3 bucket starts to fill with folders.

## How to look something up later

1. Open **CloudTrail** → **Event history**.
2. Change the filter to **User name** and type `sql-uploader`, or to **Resource name** and type your bucket name.
3. Click an event to see exactly what happened.
