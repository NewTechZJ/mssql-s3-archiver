# MSSQL S3 Backup

Backs up a Microsoft SQL Server database, checks the backup, and stores it safely in Amazon S3.

## Start here

Do the AWS setup steps **in this order**. Each step needs something from the step before it.

| Step | Guide | What you get |
|---|---|---|
| 1 | [Create the S3 bucket](docs/aws-setup/01-create-s3-bucket.md) | A private place in AWS to store the backups |
| 2 | [Create the SNS topic](docs/aws-setup/02-create-sns-topic.md) | Email alerts when a backup succeeds or fails |
| 3 | [Create the `sql-uploader` user](docs/aws-setup/03-create-iam-user.md) | A login that can only upload backups |
| 4 | [Set up the backup PC](docs/aws-setup/04-setup-backup-pc.md) | The PC can talk to AWS (`aws configure`) |
| 5 | [Turn on CloudTrail](docs/aws-setup/05-setup-cloudtrail.md) | A record of who did what in your AWS account |
| 6 | [Create a budget](docs/aws-setup/06-create-budget.md) | An email if AWS costs go higher than expected |

When AWS is ready, see [Using the tool](docs/using-the-tool.md) for settings, running backups, scheduling and restoring.

## Values to write down

Keep this list in a safe place while you work. The guides tell you when to fill in each line.

| Value | Example | Your value |
|---|---|---|
| AWS Region | `us-east-1` | |
| Bucket name | `mycompany-sql-backups` | |
| SNS topic ARN | `arn:aws:sns:us-east-1:123456789012:sql-backup-alerts` | |
| Access key ID | `AKIA...` | |
| Secret access key | *(never share or email this)* | |

## License

[MIT](LICENSE)
