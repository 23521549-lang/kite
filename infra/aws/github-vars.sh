#!/usr/bin/env bash
# Step 3 of `make bootstrap-infra`: load the GitHub variables the Infra
# workflow needs into the `aws` environment of 23521549-lang/kite.
#
# Source of truth is infra/aws/.env — step 2 (terraform-bootstrap.sh)
# collects all seven variables there. Priority for every variable:
# .env file -> shell environment -> live fallback (terraform outputs,
# public-key file, public-IP detection, built-in default). The file is
# only ever parsed line by line for the known keys, never sourced/eval'd,
# so a stray line in it cannot execute.
#
# Nothing is written until the owner confirms the full old -> new list.
# Variables already correct on GitHub are skipped. KITE_DOMAIN is only
# set when non-empty; empty means the workflow falls back to sslip.io.
#
# Knobs (environment): KITE_GH_REPO (default 23521549-lang/kite),
# KITE_GH_ENV (default aws), KITE_SSH_CIDR, KITE_SSH_PUBLIC_KEY,
# KITE_SSH_PUBLIC_KEY_FILE (default ~/.ssh/id_ed25519.pub),
# KITE_ACCESS, KITE_DOMAIN.
set -euo pipefail

REPO="${KITE_GH_REPO:-23521549-lang/kite}"
ENV_NAME="${KITE_GH_ENV:-aws}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP_DIR="$SCRIPT_DIR/bootstrap"
ENV_FILE="$SCRIPT_DIR/.env"

die() { echo "github-vars: $*" >&2; exit 1; }
say() { echo "==> $*"; }

command -v gh >/dev/null 2>&1 || die "Chưa cài GitHub CLI (gh) trên máy này."
gh auth status >/dev/null 2>&1 || die "gh chưa đăng nhập — chạy 'gh auth login' trước."

# Read one known key from .env: last matching line wins; strips a leading
# `export `, one pair of surrounding quotes and a trailing CR. Prints
# nothing when the key is absent.
env_file_get() {
    local want="$1" line key val
    [ -f "$ENV_FILE" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in '' | \#*) continue ;; esac
        line="${line#export }"
        key="${line%%=*}"
        [ "$key" = "$want" ] || continue
        val="${line#*=}"
        case "$val" in
            \"*\") val="${val:1:-1}" ;;
            \'*\') val="${val:1:-1}" ;;
        esac
        printf '%s' "$val"
    done <"$ENV_FILE" | tail -n 1
}

# pick KEY fallback...: .env first, then the shell variable of the same
# name, then the given fallback value (may be empty).
pick() {
    local key="$1" from_file from_env
    from_file="$(env_file_get "$key")"
    if [ -n "$from_file" ]; then printf '%s' "$from_file"; return; fi
    from_env="${!key:-}"
    if [ -n "$from_env" ]; then printf '%s' "$from_env"; return; fi
    printf '%s' "${2:-}"
}

tf_output() {
    command -v terraform >/dev/null 2>&1 || return 1
    terraform -chdir="$BOOTSTRAP_DIR" output -raw "$1" 2>/dev/null
}

ROLE_ARN="$(pick AWS_ROLE_ARN "$(tf_output AWS_ROLE_ARN || true)")"
[ -n "$ROLE_ARN" ] || die "Không có AWS_ROLE_ARN trong .env lẫn terraform output — chạy bước 2 trước."
OUT_REGION="$(pick AWS_REGION "$(tf_output AWS_REGION || true)")"
[ -n "$OUT_REGION" ] || die "Không có AWS_REGION trong .env lẫn terraform output — chạy bước 2 trước."
BUCKET="$(pick TF_STATE_BUCKET "$(tf_output TF_STATE_BUCKET || true)")"
[ -n "$BUCKET" ] || die "Không có TF_STATE_BUCKET trong .env lẫn terraform output — chạy bước 2 trước."

PUBKEY="$(pick KITE_SSH_PUBLIC_KEY)"
if [ -z "$PUBKEY" ]; then
    PUBKEY_FILE="${KITE_SSH_PUBLIC_KEY_FILE:-}"
    if [ -z "$PUBKEY_FILE" ]; then
        if [ -f /mnt/d/huynhngocthuan-key/id_ed25519.pub ]; then
            PUBKEY_FILE=/mnt/d/huynhngocthuan-key/id_ed25519.pub
        else
            PUBKEY_FILE="$HOME/.ssh/id_ed25519.pub"
        fi
    fi
    [ -f "$PUBKEY_FILE" ] || die "Không có KITE_SSH_PUBLIC_KEY trong .env và không thấy file key: $PUBKEY_FILE"
    PUBKEY="$(tr -d '\r\n' <"$PUBKEY_FILE")"
fi
case "$PUBKEY" in
    ssh-ed25519\ * | ssh-rsa\ *) ;;
    *) die "KITE_SSH_PUBLIC_KEY không giống public key SSH." ;;
esac

SSH_CIDR="$(pick KITE_SSH_CIDR)"
if [ -z "$SSH_CIDR" ]; then
    DETECTED_IP="$(curl -fsS https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$DETECTED_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
        die "Không có KITE_SSH_CIDR trong .env và không dò được IP public — điền vào .env rồi chạy lại."
    SSH_CIDR="$DETECTED_IP/32"
fi
[[ "$SSH_CIDR" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] ||
    die "KITE_SSH_CIDR không đúng dạng CIDR: $SSH_CIDR"
[ "$SSH_CIDR" != "0.0.0.0/0" ] || die "Từ chối 0.0.0.0/0 — SSH không mở cho toàn Internet."

ACCESS_MODE="$(pick KITE_ACCESS public)"
DOMAIN="$(pick KITE_DOMAIN)"

current_value() { gh variable get "$1" --repo "$REPO" --env "$ENV_NAME" 2>/dev/null || true; }

NAMES=(AWS_ROLE_ARN AWS_REGION TF_STATE_BUCKET KITE_SSH_CIDR KITE_SSH_PUBLIC_KEY KITE_ACCESS)
VALUES=("$ROLE_ARN" "$OUT_REGION" "$BUCKET" "$SSH_CIDR" "$PUBKEY" "$ACCESS_MODE")
if [ -n "$DOMAIN" ]; then
    NAMES+=(KITE_DOMAIN)
    VALUES+=("$DOMAIN")
fi

say "Sẽ nạp vào environment '$ENV_NAME' của $REPO:"
for i in "${!NAMES[@]}"; do
    old="$(current_value "${NAMES[$i]}")"
    if [ "$old" = "${VALUES[$i]}" ]; then
        echo "  ${NAMES[$i]}: đã đúng, bỏ qua"
    else
        echo "  ${NAMES[$i]}: '${old:-<chưa có>}' -> '${VALUES[$i]}'"
    fi
done
if [ -z "$DOMAIN" ]; then
    echo "  KITE_DOMAIN: để trống (dùng sslip.io), bỏ qua"
fi

read -r -p "Nạp các biến trên lên GitHub? [y/N] " answer
case "$answer" in
    y | Y | yes | YES) ;;
    *) die "Đã dừng, chưa nạp gì." ;;
esac

say "Tạo/cập nhật environment '$ENV_NAME' ..."
gh api -X PUT "repos/$REPO/environments/$ENV_NAME" >/dev/null

for i in "${!NAMES[@]}"; do
    if [ "$(current_value "${NAMES[$i]}")" = "${VALUES[$i]}" ]; then
        echo "  ${NAMES[$i]}: đã đúng, bỏ qua"
    else
        printf '%s' "${VALUES[$i]}" | gh variable set "${NAMES[$i]}" --repo "$REPO" --env "$ENV_NAME"
        echo "  ${NAMES[$i]}: đã nạp"
    fi
done

say "Xong. Danh sách biến hiện tại trên GitHub:"
gh variable list --repo "$REPO" --env "$ENV_NAME"
