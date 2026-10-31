# Step 6 – Create a budget (cost alert)

[← Step 5 – CloudTrail](05-setup-cloudtrail.md) · [README](../../README.md) · Optional: [Step 7 – SSM settings →](07-ssm-parameters.md)

A **budget** sends you an email when your AWS bill is getting higher than you expected. This catches mistakes early, such as a wrong setting that keeps too many backups.

**Time:** about 5 minutes
**Cost:** free

---

## 1. Work out a sensible amount

Use this rough guide for S3 storage:

| What you store | Approx. cost per month |
|---|---|
| 100 GB in S3 Standard | about $2.30 |
| 100 GB in S3 Standard-IA | about $1.25 |

**Example:** your compressed backup is 50 GB, and you keep about 9 backups (about 450 GB). That's roughly **$6–$10 a month**. A **$20** budget leaves room to spare.

*(Prices are for US East and can change. See <https://aws.amazon.com/s3/pricing/>.)*

The backup tool also writes an estimate to its log after each run, for example *"about $5.80/month for storage"*.

## 2. Create the budget

1. In the search box at the top, type **Budgets** and click it. *(It's part of **Billing and Cost Management**.)*
2. Click **Create budget**.
3. Choose **Use a template (simplified)**.
4. Choose the template **Monthly cost budget**.
5. Fill in:

| Field | What to type |
|---|---|
| **Budget name** | `monthly-aws-budget` |
| **Enter your budgeted amount** | Your amount from section 1, for example `20` |
| **Email recipients** | Your email address. Separate several addresses with commas. |

6. Click **Create budget**.

You'll now get an email when:
- your actual spend reaches **85%** of the budget,
- your actual spend reaches **100%**, and
- AWS **forecasts** you'll go over 100% this month.

> ℹ️ Budget emails can arrive up to about a day late, because AWS updates billing data a few times a day.

## ✅ Check it worked

The **Budgets** list shows `monthly-aws-budget` with your amount.

## If you get a budget alert

1. Open **Billing and Cost Management** → **Cost Explorer**.
2. Group by **Service** to see what is costing money.
3. If it's S3, open your bucket and check how many dated backup folders there are. Look at the latest run log too.

---

🎉 **AWS setup is complete.** Next, see [Using the tool](../using-the-tool.md) to run real backups and schedule them.
