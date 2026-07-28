#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# gen_bootstrap_cert.sh — generate the bootstrap TLS certificate and import it
# into AWS Certificate Manager (ACM) in BOTH regions.
#
# The bootstrap NLB terminates TLS, so its listener needs a certificate whose
# name matches bootstrap.<domain>. The sample uses a PRIVATE domain
# (bootstrap.example.internal by default), which cannot obtain a publicly
# trusted certificate — so we generate a self-signed certificate and import it.
# This follows the approach in the AWS Big Data Blog post referenced in the
# README.
#
# Because the demo is bootstrap-only, the certificate needs ONLY the
# bootstrap.<domain> name. After bootstrap the client connects directly to the
# brokers on their native Amazon DNS names, which are validated against the
# default Java truststore (Amazon public CA); the NLB certificate never fronts
# those names.
#
# Outputs:
#   certs/bootstrap.key        private key   (LOCAL ONLY, gitignored, never imported to a template)
#   certs/bootstrap.pem        self-signed certificate (also the CA/trust anchor)
#   scripts/bootstrap-ca.pem   public CA copied into the scripts asset so the
#                              EC2 client can trust the bootstrap listener
#   certs/cert-arns.env        PRIMARY_CERT_ARN / DR_CERT_ARN (sourced by deploy.sh)
#
# Idempotent: re-running reuses an existing ACM certificate with the same domain
# name rather than importing a duplicate.
#
# Usage:
#   ./gen_bootstrap_cert.sh [--domain <domain>] [--primary-region <r>] [--dr-region <r>]

set -euo pipefail

DOMAIN="${DOMAIN_NAME:-example.internal}"
PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
DR_REGION="${DR_REGION:-us-west-2}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --primary-region) PRIMARY_REGION="$2"; shift 2 ;;
        --dr-region) DR_REGION="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl is required." >&2; exit 1; }

BOOTSTRAP_NAME="bootstrap.${DOMAIN}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CERT_DIR="$REPO_ROOT/certs"
KEY_FILE="$CERT_DIR/bootstrap.key"
CERT_FILE="$CERT_DIR/bootstrap.pem"
CA_BUNDLE="$SCRIPT_DIR/bootstrap-ca.pem"   # bundled into the client scripts asset
ARNS_FILE="$CERT_DIR/cert-arns.env"

mkdir -p "$CERT_DIR"

# ── 1. Generate the self-signed certificate (local, reused if present) ───────
if [[ -f "$KEY_FILE" && -f "$CERT_FILE" ]]; then
    echo "Reusing existing certificate in $CERT_DIR (delete it to regenerate)."
else
    echo "Generating self-signed certificate for CN=$BOOTSTRAP_NAME ..."
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -subj "/CN=$BOOTSTRAP_NAME" \
        -addext "subjectAltName=DNS:$BOOTSTRAP_NAME"
    chmod 600 "$KEY_FILE"
fi

# Public trust anchor for the EC2 client (self-signed cert IS its own CA).
cp "$CERT_FILE" "$CA_BUNDLE"

# ── 2. Import into ACM in each region (idempotent by domain name) ────────────
import_cert() {  # $1 = region -> prints the certificate ARN
    local region="$1" arn

    # Reuse an existing import for this domain if one exists.
    arn=$(aws acm list-certificates --region "$region" \
        --query "CertificateSummaryList[?DomainName=='$BOOTSTRAP_NAME'].CertificateArn | [0]" \
        --output text 2>/dev/null || true)

    if [[ -n "$arn" && "$arn" != "None" ]]; then
        # Re-import onto the same ARN so a regenerated cert is picked up.
        aws acm import-certificate --region "$region" \
            --certificate-arn "$arn" \
            --certificate "fileb://$CERT_FILE" \
            --private-key "fileb://$KEY_FILE" \
            --query CertificateArn --output text >/dev/null
        echo "$arn"
        return
    fi

    aws acm import-certificate --region "$region" \
        --certificate "fileb://$CERT_FILE" \
        --private-key "fileb://$KEY_FILE" \
        --tags "Key=Project,Value=msk-dr-cross-region-custom-domain" \
        --query CertificateArn --output text
}

echo "Importing certificate into ACM ($PRIMARY_REGION) ..."
PRIMARY_CERT_ARN="$(import_cert "$PRIMARY_REGION")"
echo "  $PRIMARY_CERT_ARN"

echo "Importing certificate into ACM ($DR_REGION) ..."
DR_CERT_ARN="$(import_cert "$DR_REGION")"
echo "  $DR_CERT_ARN"

# ── 3. Emit the ARNs for deploy.sh / manual cdk deploy ───────────────────────
cat > "$ARNS_FILE" <<EOF
PRIMARY_CERT_ARN=$PRIMARY_CERT_ARN
DR_CERT_ARN=$DR_CERT_ARN
EOF

echo ""
echo "Wrote $ARNS_FILE"
echo "Pass to CDK with: -c primary_cert_arn=$PRIMARY_CERT_ARN -c dr_cert_arn=$DR_CERT_ARN"
