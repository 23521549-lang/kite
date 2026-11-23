# Kite's machine on AWS (Terraform)

`infra/aws` makes an Ubuntu 24.04 instance (t3.medium), a separate encrypted gp3 disk for `/var/lib/kite`
(never removed by a plan), an Elastic IP and a security group (443 and 80 open, 22 for your address only).
At first boot the machine mounts the disk, waits for its address and runs `deploy/stack/install.sh`; from
then on kite-stack keeps Kite updated. The machine gets no AWS role. It runs from GitHub Actions without
any AWS key: the workflow signs in with GitHub's OIDC token.

## Once: the state bucket and the role (from your laptop, with your own AWS credentials)

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
