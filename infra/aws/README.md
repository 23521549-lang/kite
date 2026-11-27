# Kite's machine on AWS (Terraform)

`infra/aws` makes an Ubuntu 24.04 instance (t3.medium) in its own VPC, a separate encrypted gp3 disk for
`/var/lib/kite` (never removed by a plan), an Elastic IP and a security group (443 open, 80 open in
public mode only, 22 for your address only).
At first boot the machine mounts the disk, waits for its address and runs `deploy/stack/install.sh`; from
then on kite-stack keeps Kite updated. The machine gets no AWS role. It runs from GitHub Actions without
any AWS key: the workflow signs in with GitHub's OIDC token.

## Once: the state bucket and the role (from your laptop, with your own AWS credentials)

With the AWS CLI v2, Terraform and `gh` (signed in) on the laptop:

```sh
make bootstrap-infra
# key elsewhere:  KITE_SSH_PUBLIC_KEY_FILE=/path/to/key.pub make bootstrap-infra
# other region:   KITE_AWS_REGION=us-east-1 make bootstrap-infra
```

Three steps, each asking before it changes anything: `aws-profile.sh` checks (or saves) the `kite` AWS
profile, `terraform-bootstrap.sh` applies `infra/aws/bootstrap` with it and collects the seven variables
below into `infra/aws/.env` (git-ignored, 0600), `github-vars.sh` sets them on the `aws` environment.
Knobs: `KITE_AWS_PROFILE` (default `kite`), `KITE_AWS_REGION` (default `ap-southeast-1`),
`KITE_SSH_PUBLIC_KEY_FILE` (default `~/.ssh/kite.pub`, else `~/.ssh/id_ed25519.pub`), `KITE_SSH_CIDR`.
Keep `infra/aws/bootstrap/terraform.tfstate`: it is the only record of the bucket and the role.

By hand instead:

```sh
terraform -chdir=infra/aws/bootstrap init
terraform -chdir=infra/aws/bootstrap apply -var region=us-east-1
```

If the account already has GitHub's OIDC provider, add `-var create_oidc_provider=false`. Then in the
repository: Settings > Environments > New environment `aws` (add yourself as required reviewer if you
want a confirmation before each run), and Settings > Secrets and variables > Actions > Variables:

| Variable | Value |
|---|---|
| `AWS_ROLE_ARN`, `AWS_REGION`, `TF_STATE_BUCKET` | the outputs of the bootstrap |
| `KITE_SSH_CIDR` | your address for SSH, e.g. `203.0.113.7/32` |
| `KITE_SSH_PUBLIC_KEY` | one line: `ssh-ed25519 AAAA... you@laptop` |
| `KITE_ACCESS`, `KITE_DOMAIN` | optional: `private`; your own domain pointing at the address |

## After bootstrap: checklist

- **Turn on required reviewers for the `aws` environment** (Settings > Environments > `aws` >
  Required reviewers: yourself). The role from the bootstrap can only be assumed from that
  environment, and with reviewers on, no Infra workflow run can apply anything to AWS until you
  approve it in GitHub.
- **Re-run step 2 whenever the bootstrap policy changes.** The role's permissions live in
  `infra/aws/bootstrap/main.tf`; editing that file does nothing until the bootstrap is applied
  again (`make bootstrap-infra`, or just `bash infra/aws/terraform-bootstrap.sh`). The same
  applies when rotating back: policy changes only take effect through that re-run.

## The machine

Actions > Infra > Run workflow: `plan` shows what would change, `apply` makes it. The outputs give the
address, the page and the SSH command. About 10 minutes after the first apply:

```sh
ssh ubuntu@<address>
sudo tail -f /var/log/kite-install.log      # until "done"
sudo kite-stack admin
```

Replacing the instance keeps the data disk; the new instance installs Kite again on top of it. Removing
everything needs the disk's `prevent_destroy` taken out first, on purpose.

The first boot runs `install.sh` from a fixed commit (`installer_url` in `variables.tf`), never from the
moving `main`, so a change pushed later cannot alter what a new machine runs as root. After changing
`deploy/stack/install.sh`, push it, then pin the new commit:

```sh
sha=$(git rev-parse HEAD)   # the pushed commit with the new install.sh
sed -i -E "s#(kite/)[0-9a-f]{40}(/deploy/stack/install.sh)#\1${sha}\2#" infra/aws/variables.tf
```

The installer installs cosign (the release CI signs with, checked against its SHA-256): a new machine
runs an image only when it carries this repository's CI signature.

## Backups: daily snapshots of the data disk

The bootstrap makes a Data Lifecycle Manager policy: every day at 19:00 UTC AWS snapshots the volume
tagged `Backup = kite-daily` (the data disk, `/var/lib/kite`) and keeps the newest 7. The policy and the
role DLM uses live in `infra/aws/bootstrap`, so a machine made before this needs the bootstrap applied
again (`bash infra/aws/terraform-bootstrap.sh`) and one Infra `apply` for the tag. The snapshots are
encrypted like the disk and cost EBS snapshot storage (incremental after the first). A snapshot is
crash-consistent: Postgres recovers from it the way it does after a power cut.

The snapshots so far (newest last):

```sh
aws ec2 describe-snapshots --profile kite --owner-ids self --filters Name=tag:Backup,Values=kite-daily \
  --query 'sort_by(Snapshots,&StartTime)[].[SnapshotId,StartTime,State]' --output table
```

### Restore drill (once, then after big changes)

A copy of the newest snapshot is attached to the running machine next to the real disk, read, and
deleted; Kite keeps running. From the laptop:

```sh
snap=$(aws ec2 describe-snapshots --profile kite --owner-ids self --filters Name=tag:Backup,Values=kite-daily \
  --query 'sort_by(Snapshots,&StartTime)[-1].SnapshotId' --output text)
id=$(aws ec2 describe-instances --profile kite --filters Name=tag:Name,Values=kite \
  Name=instance-state-name,Values=running --query 'Reservations[0].Instances[0].InstanceId' --output text)
az=$(aws ec2 describe-instances --profile kite --instance-ids "$id" \
  --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text)
vol=$(aws ec2 create-volume --profile kite --snapshot-id "$snap" --availability-zone "$az" --volume-type gp3 \
  --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=kite-restore-drill}]' --query VolumeId --output text)
aws ec2 wait volume-available --profile kite --volume-ids "$vol"
aws ec2 attach-volume --profile kite --volume-id "$vol" --instance-id "$id" --device /dev/sdg
echo "$vol"
```

The copy carries no `Backup` tag, so DLM never snapshots it. It has the same filesystem id as the real
disk: mount it by its device path as below, and never reboot the machine with it attached. On the
machine (`vol-0abc...` is the id printed above):

```sh
dev=/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_$(echo vol-0abc... | tr -d -)
sudo mkdir -p /mnt/restore && sudo mount "$dev" /mnt/restore
sudo ls /mnt/restore/kite /mnt/restore/postgres          # config.yaml, secrets/, journal/, PG_VERSION ...
sudo du -sh /mnt/restore /var/lib/kite                   # about the same size as the live disk
sudo umount /mnt/restore
```

Then from the laptop: `aws ec2 detach-volume --profile kite --volume-id "$vol"`,
`aws ec2 wait volume-available --profile kite --volume-ids "$vol"`,
`aws ec2 delete-volume --profile kite --volume-id "$vol"`.

### Real restore (the data disk is damaged or files are lost)

Same first steps (copy of the chosen snapshot, attached, mounted at `/mnt/restore`), then on the
machine, with Kite stopped so nothing writes while the files come back:

```sh
sudo systemctl stop kite-stack-update.timer kite-stack-watchdog.timer
sudo kite-stack down
sudo rsync -aHAX --delete --exclude /lost+found /mnt/restore/ /var/lib/kite/
sudo umount /mnt/restore
sudo kite-stack up && sudo kite-stack status
sudo systemctl start kite-stack-update.timer kite-stack-watchdog.timer
```

The Terraform disk stays the same volume (its `prevent_destroy` is untouched); only its files go back
to the snapshot's day. Detach and delete the copy as in the drill.
