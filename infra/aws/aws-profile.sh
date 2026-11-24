#!/usr/bin/env bash
# Step 1 of `make bootstrap-infra`: make sure this machine has an AWS profile
# for Kite (default name: kite) that really works, before Terraform runs.
#
# What it does:
#   * profile already there and its key still works -> show which account and
#     user it is, ask the owner to confirm, done;
#   * profile missing or its key dead -> ask for a new access key (the secret
#     is typed hidden, never printed, never logged) and save it with
#     `aws configure set`, then check again.
# It never creates an IAM user and never touches AWS: the key must come from
# an IAM user the owner made (for Kite: user `kite-terraform`).
#
# Knobs (environment): KITE_AWS_PROFILE (default kite),
# KITE_AWS_REGION (default ap-southeast-1, must match the bootstrap -var).
set -euo pipefail

PROFILE="${KITE_AWS_PROFILE:-kite}"
REGION="${KITE_AWS_REGION:-ap-southeast-1}"

die() { echo "aws-profile: $*" >&2; exit 1; }
say() { echo "==> $*"; }

command -v aws >/dev/null 2>&1 || die "AWS CLI chưa có trên máy này: cài AWS CLI v2 rồi chạy lại."

# Print "Account <id> | <arn>" for the profile, or nothing when its key fails.
identity() {
    local account arn
    account="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text 2>/dev/null)" || return 1
    arn="$(aws sts get-caller-identity --profile "$PROFILE" --query Arn --output text 2>/dev/null)" || return 1
    [ -n "$account" ] && [ -n "$arn" ] || return 1
    echo "Account $account | $arn"
}

# Show an identity line and ask the owner to confirm it is the right account.
confirm_identity() {
    local who="$1" answer
    say "Profile '$PROFILE' đang trỏ tới: $who"
    read -r -p "Đúng tài khoản AWS của bạn cho Kite? [y/N] " answer
    case "$answer" in
        y | Y | yes | YES) ;;
        *) die "Đã dừng. Kiểm tra lại key/profile rồi chạy lại." ;;
    esac
}

if aws configure list-profiles 2>/dev/null | grep -qx "$PROFILE"; then
    if who="$(identity)"; then
        say "Profile '$PROFILE' đã có sẵn và key còn dùng được."
        confirm_identity "$who"
        say "Bước 1 xong: dùng profile '$PROFILE'."
        exit 0
    fi
    say "Profile '$PROFILE' đã có nhưng key không dùng được nữa (hết hạn hoặc bị thu hồi) — khai lại key mới."
else
    say "Chưa có profile '$PROFILE'. Các profile hiện có:"
    aws configure list-profiles 2>/dev/null || true
fi

read -r -p "Access Key ID cho profile '$PROFILE': " access_key
[ -n "$access_key" ] || die "Access Key ID rỗng."
read -r -s -p "Secret Access Key (gõ không hiện): " secret_key
echo
[ -n "$secret_key" ] || die "Secret Access Key rỗng."

aws configure set aws_access_key_id "$access_key" --profile "$PROFILE"
aws configure set aws_secret_access_key "$secret_key" --profile "$PROFILE"
aws configure set region "$REGION" --profile "$PROFILE"
aws configure set output json --profile "$PROFILE"
unset access_key secret_key

who="$(identity)" || die "Key vừa lưu vẫn không gọi được AWS — sai key, sai secret, hoặc user đã bị khoá. Chưa sửa gì thêm."
confirm_identity "$who"
say "Bước 1 xong: profile '$PROFILE' (region $REGION) đã sẵn sàng. Các bước sau nhớ dùng AWS_PROFILE=$PROFILE."
