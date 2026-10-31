# Step 2 – Create the SNS topic (email alerts)

[← Step 1 – S3 bucket](01-create-s3-bucket.md) · [README](../../README.md) · Next: [Step 3 – IAM user →](03-create-iam-user.md)

**SNS** sends you an email after every backup, telling you whether it succeeded or failed.

**Time:** about 5 minutes

---

## 1. Create the topic

1. Check that the **Region** in the top-right corner is the same one you used in Step 1.
2. In the search box at the top, type **SNS** and click **Simple Notification Service**.
3. In the left menu, click **Topics**, then **Create topic**.
4. Fill in:
   - **Type:** **Standard**
   - **Name:** `sql-backup-alerts`
5. Leave everything else as it is. Click **Create topic** at the bottom.

## 2. Copy the topic ARN

On the next page you'll see **ARN**. It looks like this:

```
arn:aws:sns:us-east-1:123456789012:sql-backup-alerts
```

> ✏️ **Write down** the full ARN. You'll need it in Step 3 and in `config.json`.

## 3. Add your email address

1. On the same page, click **Create subscription**.
2. Fill in:
   - **Protocol:** **Email**
   - **Endpoint:** the email address that should get the alerts
3. Click **Create subscription**.
4. **Open your email inbox.** You'll get an email from *AWS Notifications*. Click **Confirm subscription** in it.
   *(Check your spam folder if you can't find it.)*

To send alerts to more people, repeat this section for each email address.

## ✅ Check it worked

1. Go back to the topic and click **Publish message** (top right).
2. **Subject:** `Test`. **Message body:** `Hello`.
3. Click **Publish message**.
4. You should get the email within a minute.

The subscription status should say **Confirmed**, not *Pending confirmation*.
