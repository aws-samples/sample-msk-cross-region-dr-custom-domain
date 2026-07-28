#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# destroy.sh — tear down all stacks in the correct reverse order.
#
# Usage:
#   ./destroy.sh               # destroy everything (prompts for confirmation)
#   ./destroy.sh --force       # skip confirmation prompts

set -euo pipefail

PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
DR_REGION="${DR_REGION:-us-west-2}"
DOMAIN_NAME="${DOMAIN_NAME:-example.internal}"

_FORCE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) _FORCE="--force"; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

echo "============================================================"
echo " MSK Cross-Region DR Demo — Teardown"
echo "============================================================"
echo ""
echo "This will destroy ALL stacks in both regions."
if [[ -z "$_FORCE" ]]; then
    echo -n "Are you sure? (y/N) "
    read -r confirm
    [[ "$confirm" =~ ^[Yy] ]] || { echo "Aborted."; exit 0; }
fi
echo ""

echo "[1/4] Destroying Replicator + DNS..."
cdk destroy MskReplicator MskDnsFailover $_FORCE 2>/dev/null || true

echo "[2/4] Destroying networking (peering, then TGWs)..."
cdk destroy TgwPeeringAccepter TgwPeering $_FORCE 2>/dev/null || true
sleep 10  # Allow peering attachment to fully delete
cdk destroy TgwDr TgwPrimary $_FORCE 2>/dev/null || true

echo "[3/4] Destroying Client + Clusters..."
cdk destroy MskClient MskDrCluster MskPrimaryCluster $_FORCE 2>/dev/null || true

echo "[4/4] Destroying ARC routing controls..."
cdk destroy MskRoutingControls $_FORCE 2>/dev/null || true

# ── Delete the imported bootstrap certificates ───────────────────────────────
# Run AFTER the cluster stacks (and their NLB listeners) are gone; ACM refuses to
# delete a certificate that is still associated with a listener.
echo "[cert] Deleting imported bootstrap certificates from ACM..."
BOOTSTRAP_NAME="bootstrap.${DOMAIN_NAME}"
for region in "$PRIMARY_REGION" "$DR_REGION"; do
    arn=$(aws acm list-certificates --region "$region" \
        --query "CertificateSummaryList[?DomainName=='$BOOTSTRAP_NAME'].CertificateArn | [0]" \
        --output text 2>/dev/null || true)
    if [[ -n "$arn" && "$arn" != "None" ]]; then
        aws acm delete-certificate --region "$region" --certificate-arn "$arn" 2>/dev/null \
            && echo "  deleted $arn ($region)" \
            || echo "  WARNING: could not delete $arn ($region) — it may still be in use; retry shortly."
    fi
done
rm -f certs/cert-arns.env

echo ""
echo "============================================================"
echo " Teardown complete. All resources destroyed."
echo "============================================================"
