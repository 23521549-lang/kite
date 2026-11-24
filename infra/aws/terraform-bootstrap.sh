#!/usr/bin/env bash
# Step 2 of `make bootstrap-infra`: run the Terraform bootstrap in
# infra/aws/bootstrap (S3 state bucket + GitHub OIDC provider + infra role)
# with the `kite` AWS profile, then collect ALL SEVEN GitHub variables into
# infra/aws/.env — step 3 (github-vars.sh) loads GitHub from that file only.
#
# What lands in .env:
#   AWS_ROLE_ARN, AWS_REGION, TF_STATE_BUCKET  <- terraform outputs
#   KITE_SSH_CIDR        <- this machine's current public IP + /32
#   KITE_SSH_PUBLIC_KEY  <- the owner's SSH public key file
#   KITE_ACCESS          <- default `public`
#   KITE_DOMAIN          <- default empty (sslip.io)
# A variable already holding a value in .env is KEPT, never overwritten —
# the fills above only apply to missing/empty ones (the three terraform
# outputs are the exception: they always refresh from the live apply).
#
# Flow: check the profile -> init -> plan -> owner confirms -> apply
# (-auto-approve only after that confirm). If apply fails only because the
# account already has GitHub's OIDC provider, it retries once with
# -var create_oidc_provider=false. Any other failure stops; values already
# written stay written, the rest is reported as missing at the end.
#
# These are Actions variables, not secrets — but .env stays git-ignored
# and 0600 anyway, because the same file holds the owner's other settings.
#
# Knobs (environment): KITE_AWS_PROFILE (default kite),
# KITE_AWS_REGION (default ap-southeast-1), KITE_SSH_CIDR (skip detection),
# KITE_SSH_PUBLIC_KEY_FILE (default: /mnt/d/huynhngocthuan-key/id_ed25519.pub
# when present, else ~/.ssh/id_ed25519.pub).
set -euo pipefail

PROFILE="${KITE_AWS_PROFILE:-kite}"
REGION="${KITE_AWS_REGION:-ap-southeast-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP_DIR="$SCRIPT_DIR/bootstrap"
ENV_FILE="$SCRIPT_DIR/.env"

die() { echo "terraform-bootstrap: $*" >&2; exit 1; }
say() { echo "==> $*"; }
warn() { echo "!! $*" >&2; }

command -v terraform >/dev/null 2>&1 || die "Chưa cài Terraform trên máy này."
command -v aws >/dev/null 2>&1 || die "Chưa cài AWS CLI trên máy này (bước 1 cần nó)."
export AWS_PROFILE="$PROFILE"

say "Kiểm tra profile '$PROFILE' ..."
aws sts get-caller-identity --profile "$PROFILE" >/dev/null 2>&1 ||
    die "Profile '$PROFILE' không gọi được AWS — chạy bước 1 (aws-profile.sh) trước."
aws sts get-caller-identity --profile "$PROFILE" --query '{Account:Account, Arn:Arn}' --output table

say "terraform init (lần đầu tải provider AWS, hơi lâu) ..."
terraform -chdir="$BOOTSTRAP_DIR" init -input=false

say "terraform plan ..."
terraform -chdir="$BOOTSTRAP_DIR" plan -input=false -var "region=$REGION"

read -r -p "Apply plan trên vào tài khoản AWS của bạn? [y/N] " answer
case "$answer" in
    y | Y | yes | YES) ;;
    *) die "Đã dừng, chưa apply gì." ;;
esac

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
apply_tf() {
    terraform -chdir="$BOOTSTRAP_DIR" apply -input=false -auto-approve "$@" 2>&1 | tee "$LOG"
}
say "terraform apply ..."
if ! apply_tf -var "region=$REGION"; then
    if grep -q "EntityAlreadyExists" "$LOG"; then
        say "Account đã có sẵn GitHub OIDC provider — apply lại với create_oidc_provider=false ..."
        apply_tf -var "region=$REGION" -var "create_oidc_provider=false" ||
            die "Apply lại vẫn lỗi — xem output ở trên."
    else
        die "Apply lỗi — xem output ở trên."
    fi
fi

tf_output() { terraform -chdir="$BOOTSTRAP_DIR" output -raw "$1" 2>/dev/null; }
ROLE_ARN="$(tf_output AWS_ROLE_ARN)" || die "Apply xong nhưng không đọc được output AWS_ROLE_ARN."
OUT_REGION="$(tf_output AWS_REGION)" || die "Apply xong nhưng không đọc được output AWS_REGION."
BUCKET="$(tf_output TF_STATE_BUCKET)" || die "Apply xong nhưng không đọc được output TF_STATE_BUCKET."
[ -n "$ROLE_ARN" ] && [ -n "$OUT_REGION" ] && [ -n "$BUCKET" ] || die "Có output rỗng — dừng tại đây."

# --- .env helpers ---------------------------------------------------------
# Set KEY=value in .env: replace the existing line or append; keep every
# other line exactly as is. Value is written raw (quote it before calling
# when it contains spaces).
set_env_var() {
    local key="$1" value="$2" tmp found=0 line
    tmp="$(mktemp)"
    [ -f "$ENV_FILE" ] || : >"$ENV_FILE"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "$key="*) printf '%s=%s\n' "$key" "$value" >>"$tmp"; found=1 ;;
            *) printf '%s\n' "$line" >>"$tmp" ;;
        esac
    done <"$ENV_FILE"
    [ "$found" = 1 ] || printf '%s=%s\n' "$key" "$value" >>"$tmp"
    mv "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

# Print the current value of KEY in .env (quotes stripped), empty if unset.
get_env_var() {
    local key="$1" raw=""
    [ -f "$ENV_FILE" ] || { printf ''; return; }
    raw="$(grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
    raw="${raw%$'\r'}"
    case "$raw" in
        \"*\") raw="${raw:1:-1}" ;;
        \'*\') raw="${raw:1:-1}" ;;
    esac
    printf '%s' "$raw"
}

MISSING=""

# The three terraform outputs always refresh.
set_env_var AWS_ROLE_ARN "$ROLE_ARN"
set_env_var AWS_REGION "$OUT_REGION"
set_env_var TF_STATE_BUCKET "$BUCKET"

# KITE_SSH_CIDR: keep an existing value; else $KITE_SSH_CIDR; else detect.
if [ -n "$(get_env_var KITE_SSH_CIDR)" ]; then
    say "KITE_SSH_CIDR đã có trong .env, giữ nguyên: $(get_env_var KITE_SSH_CIDR)"
elif [ -n "${KITE_SSH_CIDR:-}" ]; then
    set_env_var KITE_SSH_CIDR "$KITE_SSH_CIDR"
else
    DETECTED_IP="$(curl -fsS https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$DETECTED_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        set_env_var KITE_SSH_CIDR "$DETECTED_IP/32"
        say "KITE_SSH_CIDR tự dò: $DETECTED_IP/32"
    else
        warn "Không dò được IP public — KITE_SSH_CIDR chưa có trong .env."
        MISSING="$MISSING KITE_SSH_CIDR"
    fi
fi

# KITE_SSH_PUBLIC_KEY: keep an existing value; else read the key file.
if [ -n "$(get_env_var KITE_SSH_PUBLIC_KEY)" ]; then
    say "KITE_SSH_PUBLIC_KEY đã có trong .env, giữ nguyên."
else
    PUBKEY_FILE="${KITE_SSH_PUBLIC_KEY_FILE:-}"
    if [ -z "$PUBKEY_FILE" ]; then
        if [ -f /mnt/d/huynhngocthuan-key/id_ed25519.pub ]; then
            PUBKEY_FILE=/mnt/d/huynhngocthuan-key/id_ed25519.pub
        else
            PUBKEY_FILE="$HOME/.ssh/id_ed25519.pub"
        fi
    fi
    if [ -f "$PUBKEY_FILE" ]; then
        PUBKEY="$(tr -d '\r\n' <"$PUBKEY_FILE")"
        case "$PUBKEY" in
            ssh-ed25519\ * | ssh-rsa\ *)
                set_env_var KITE_SSH_PUBLIC_KEY "\"$PUBKEY\""
                say "KITE_SSH_PUBLIC_KEY đã lấy từ $PUBKEY_FILE"
                ;;
            *) warn "File $PUBKEY_FILE không giống public key — bỏ qua."; MISSING="$MISSING KITE_SSH_PUBLIC_KEY" ;;
        esac
    else
        warn "Không thấy file public key ($PUBKEY_FILE) — KITE_SSH_PUBLIC_KEY chưa có trong .env."
        MISSING="$MISSING KITE_SSH_PUBLIC_KEY"
    fi
fi

# KITE_ACCESS / KITE_DOMAIN: defaults only when missing or empty.
if [ -z "$(get_env_var KITE_ACCESS)" ]; then
    set_env_var KITE_ACCESS public
fi
if ! grep -q '^KITE_DOMAIN=' "$ENV_FILE" 2>/dev/null; then
    set_env_var KITE_DOMAIN ""
fi

say "Bước 2 xong. Nội dung $ENV_FILE:"
grep -E '^(AWS_ROLE_ARN|AWS_REGION|TF_STATE_BUCKET|KITE_SSH_CIDR|KITE_SSH_PUBLIC_KEY|KITE_ACCESS|KITE_DOMAIN)=' "$ENV_FILE" |
    sed 's/^/  /' || true
if [ -n "$MISSING" ]; then
    warn "CHƯA ĐỦ — còn thiếu:$MISSING. Điền tay vào .env rồi chạy bước 3."
else
    say "Đã đủ 7 biến trong .env — chạy bước 3 (github-vars.sh) để nạp lên GitHub."
fi
say "State của bootstrap nằm trong $BOOTSTRAP_DIR — đừng xoá thư mục đó."
