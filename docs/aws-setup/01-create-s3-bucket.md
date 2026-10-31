# Step 1 – Create the S3 bucket

[← Back to README](../../README.md) · Next: [Step 2 – SNS topic →](02-create-sns-topic.md)

An **S3 bucket** is like a folder in the cloud. Your backup files will be stored here.

**Time:** about 10 minutes
**You need:** an AWS login with administrator rights

---

## 1. Sign in and pick your Region

1. Go to <https://console.aws.amazon.com> and sign in.
2. Look at the **top-right corner** of the page. You'll see a Region name, such as **N. Virginia** or **Ohio**.
3. Click it and choose the Region **closest to your office**.

> ✏️ **Write down** the Region code shown next to the name (for example `us-east-1`).
> Use the **same Region** in every step of these guides.

## 2. Open S3

1. In the search box at the top, type **S3**.
2. Click **S3** in the results.
3. Click the orange **Create bucket** button.

## 3. Fill in the form

Go down the page from top to bottom:

| Field | What to choose |
|---|---|
| **Bucket type** | **General purpose** |
| **Bucket name** | A unique name in lowercase, with no spaces. Example: `mycompany-sql-backups` |
| **Object Ownership** | Leave it as **ACLs disabled (recommended)** |
| **Block Public Access settings** | Make sure **Block *all* public access** is ✅ **ticked** |
| **Bucket Versioning** | Choose **Enable** |
| **Default encryption** | **Server-side encryption with Amazon S3 managed keys (SSE-S3)** |
| **Bucket Key** | **Enable** |

Leave everything else as it is. Click **Create bucket** at the bottom.

> ✏️ **Write down** the bucket name.

> ⚠️ Bucket names must be unique across all AWS customers. If you see "Bucket with the same name already exists", add something to it, such as `-2026`.

## 4. (Recommended) Allow only secure connections

This blocks anyone from connecting to the bucket without encryption.

1. Click your new bucket's name.
2. Open the **Permissions** tab.
3. Under **Bucket policy**, click **Edit**.
4. Paste the text below. Replace **both** places that say `YOUR-BUCKET-NAME` with your bucket name.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::YOUR-BUCKET-NAME",
        "arn:aws:s3:::YOUR-BUCKET-NAME/*"
      ],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
```

5. Click **Save changes**.

## ✅ Check it worked

Open the bucket and check the following:
- **Properties** tab → **Bucket Versioning** says **Enabled**.
- **Properties** tab → **Default encryption** says **SSE-S3**.
- **Permissions** tab → **Block public access** says **On**.

You don't need to set up any lifecycle rules. The backup tool creates them itself.
