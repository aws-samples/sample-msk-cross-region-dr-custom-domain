#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# deploy.sh — one-command deployment of the entire cross-region MSK DR demo.
#
# Orchestrates the 10 CDK stacks in the correct order, waits for each
# prerequisite state (cluster ACTIVE, peering available, etc.), runs the three
# post-deploy wiring steps, and is idempotent (safe to re-run on partial failure).
#
# Prerequisites:
#   - AWS credentials with AdministratorAccess in the target account
#   - CDK CLI >= 2.1128.1 (check with: cdk --version)
#   - Python deps installed: pip install -r requirements.txt
#   - CDK bootstrapped in BOTH regions:
#       cdk bootstrap aws://<ACCOUNT>/us-east-1 aws://<ACCOUNT>/us-west-2
#
# Usage:
#   ./deploy.sh                         # deploy everything
#   ./deploy.sh --account 123456789012  # override account (else uses STS)
#
# Teardown: ./destroy.sh (or see README "Clean up" section)

set -euo pipefail

PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
DR_REGION="${DR_REGION:-us-west-2}"
ACCOUNT=""
_APPROVE="--require-approval never"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --account) ACCOUNT="$2"; shift 2 ;;
        --interactive) _APPROVE=""; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# Resolve account from STS if not provided.
if [[ -z "$ACCOUNT" ]]; then
    ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
fi
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"

echo "============================================================"
echo " MSK Cross-Region DR Demo — Deployment"
echo "============================================================"
echo "  Account:        $ACCOUNT"
echo "  Primary region: $PRIMARY_REGION"
echo "  DR region:      $DR_REGION"
echo "============================================================"
echo ""

_CDK_CONTEXT=""

# ── Bootstrap TLS certificate ────────────────────────────────────────────────
# The bootstrap NLB terminates TLS, so it needs an ACM certificate for
# bootstrap.<domain> in EACH region. Generate + import it before any cdk deploy,
# then pass the per-region ARNs into every stack via context. Idempotent: reuses
# an existing certificate/import for the same domain.
DOMAIN_NAME="${DOMAIN_NAME:-example.internal}"
echo "[cert] Generating + importing bootstrap TLS certificate for bootstrap.$DOMAIN_NAME ..."
DOMAIN_NAME="$DOMAIN_NAME" PRIMARY_REGION="$PRIMARY_REGION" DR_REGION="$DR_REGION" \
    scripts/gen_bootstrap_cert.sh
# shellcheck disable=SC1091
source certs/cert-arns.env
_CDK_CONTEXT="$_CDK_CONTEXT -c primary_cert_arn=$PRIMARY_CERT_ARN -c dr_cert_arn=$DR_CERT_ARN"
echo ""

# Helper: wait for an MSK cluster to reach ACTIVE state.
wait_cluster_active() {  # $1 = cluster ARN, $2 = region, $3 = label
    local arn="$1" region="$2" label="$3"
    echo -n "  Waiting for $label to become ACTIVE"
    for _ in $(seq 1 60); do
        state=$(aws kafka describe-cluster-v2 --region "$region" --cluster-arn "$arn" \
            --query 'ClusterInfo.State' --output text 2>/dev/null || echo "?")
        [[ "$state" == "ACTIVE" ]] && { echo " ✓"; return 0; }
        echo -n "."
        sleep 30
    done
    echo " TIMEOUT (state=$state)"
    return 1
}

# Helper: get a CloudFormation stack output.
stack_output() {  # $1 = stack, $2 = key, $3 = region
    aws cloudformation describe-stacks --region "$3" --stack-name "$1" \
        --query "Stacks[0].Outputs[?OutputKey=='${2}'].OutputValue" \
        --output text 2>/dev/null || true
}

# ── Step 0: ARC routing controls ────────────────────────────────────────────
# Deployed FIRST: the DNS and client stacks reference the primary routing-control
# ARN. New routing controls default to Off, so set it On before the DNS health
# check points at it, or DNS reads the primary as unhealthy and fails over at once.
echo "[0/7] Deploying ARC routing controls (us-west-2)..."
cdk deploy MskRoutingControls $_APPROVE $_CDK_CONTEXT
echo "  Setting primary routing control to On (steady state)..."
scripts/set_routing_control.sh --state On \
    --routing-control-arn "$(stack_output MskRoutingControls PrimaryRoutingControlArn "$DR_REGION")" \
    --cluster-arn "$(stack_output MskRoutingControls ArcClusterArn "$DR_REGION")"
echo ""

# ── Step 1: Clusters + Client ────────────────────────────────────────────────
echo "[1/7] Deploying MSK clusters (both regions) + client VPC..."
cdk deploy MskPrimaryCluster MskDrCluster MskClient $_APPROVE $_CDK_CONTEXT
echo ""

# ── Step 2: Transit Gateways + Peering ───────────────────────────────────────
echo "[2/7] Deploying Transit Gateways..."
cdk deploy TgwPrimary TgwDr $_APPROVE $_CDK_CONTEXT
echo "  Deploying TGW peering (initiator)..."
cdk deploy TgwPeering $_APPROVE $_CDK_CONTEXT
echo "  Deploying TGW peering (accepter)..."
cdk deploy TgwPeeringAccepter $_APPROVE $_CDK_CONTEXT
echo ""

# ── Step 3: Wire TGW routes ──────────────────────────────────────────────────
echo "[3/7] Wiring cross-region TGW routes..."
scripts/wire_tgw_routes.sh
echo ""

# ── Step 4: DNS / Failover ───────────────────────────────────────────────────
echo "[4/7] Deploying Route 53 DNS failover..."
cdk deploy MskDnsFailover $_APPROVE $_CDK_CONTEXT
echo ""

# ── Step 5: Enable multi-VPC on primary cluster ──────────────────────────────
echo "[5/7] Enabling multi-VPC connectivity on primary cluster..."
PRIMARY_CLUSTER_ARN=$(stack_output MskPrimaryCluster ClusterArn "$PRIMARY_REGION")
wait_cluster_active "$PRIMARY_CLUSTER_ARN" "$PRIMARY_REGION" "primary cluster"
scripts/enable_source_multivpc.sh
echo "  Waiting for primary cluster to return to ACTIVE after update..."
sleep 10
wait_cluster_active "$PRIMARY_CLUSTER_ARN" "$PRIMARY_REGION" "primary cluster (post multi-VPC)"
echo ""

# ── Step 6: Cross-region Replicator ──────────────────────────────────────────
echo "[6/7] Deploying MSK Replicator (us-west-2)..."
cdk deploy MskReplicator $_APPROVE $_CDK_CONTEXT
echo ""

# ── Step 7: Register broker targets with NLBs ────────────────────────────────
echo "[7/7] Registering broker targets with NLBs..."
echo "  This step runs on the EC2 client via SSM (brokers must resolve in-VPC)."

CLIENT_INSTANCE=$(stack_output MskClient InstanceId "$PRIMARY_REGION")
if [[ -z "$CLIENT_INSTANCE" || "$CLIENT_INSTANCE" == "None" ]]; then
    echo "  WARNING: Could not resolve client instance ID from stack."
    echo "  Run manually from the client:  /opt/kafka/register_broker_targets.sh --cluster primary"
    echo "                                 /opt/kafka/register_broker_targets.sh --cluster dr"
else
    # Wait for SSM agent to come online.
    echo -n "  Waiting for SSM agent on $CLIENT_INSTANCE"
    for _ in $(seq 1 20); do
        ssm_status=$(aws ssm describe-instance-information --region "$PRIMARY_REGION" \
            --filters "Key=InstanceIds,Values=$CLIENT_INSTANCE" \
            --query "InstanceInformationList[0].PingStatus" --output text 2>/dev/null || echo "?")
        [[ "$ssm_status" == "Online" ]] && { echo " ✓"; break; }
        echo -n "."; sleep 15
    done

    echo "  Registering primary cluster targets..."
    CMD_ID=$(aws ssm send-command --region "$PRIMARY_REGION" \
        --instance-ids "$CLIENT_INSTANCE" \
        --document-name "AWS-RunShellScript" \
        --parameters 'commands=["bash -lc \"/opt/kafka/register_broker_targets.sh --cluster primary\""],executionTimeout=["300"]' \
        --query "Command.CommandId" --output text)
    aws ssm wait command-executed --region "$PRIMARY_REGION" \
        --command-id "$CMD_ID" --instance-id "$CLIENT_INSTANCE" 2>/dev/null || true
    echo "    $(aws ssm get-command-invocation --region "$PRIMARY_REGION" \
        --command-id "$CMD_ID" --instance-id "$CLIENT_INSTANCE" \
        --query Status --output text 2>/dev/null)"

    echo "  Registering DR cluster targets..."
    CMD_ID=$(aws ssm send-command --region "$PRIMARY_REGION" \
        --instance-ids "$CLIENT_INSTANCE" \
        --document-name "AWS-RunShellScript" \
        --parameters 'commands=["bash -lc \"/opt/kafka/register_broker_targets.sh --cluster dr\""],executionTimeout=["300"]' \
        --query "Command.CommandId" --output text)
    aws ssm wait command-executed --region "$PRIMARY_REGION" \
        --command-id "$CMD_ID" --instance-id "$CLIENT_INSTANCE" 2>/dev/null || true
    echo "    $(aws ssm get-command-invocation --region "$PRIMARY_REGION" \
        --command-id "$CMD_ID" --instance-id "$CLIENT_INSTANCE" \
        --query Status --output text 2>/dev/null)"
fi

echo ""
echo "============================================================"
echo " Deployment complete!"
echo "============================================================"
echo ""
echo "Connect to the client via SSM Session Manager:"
echo "  aws ssm start-session --region $PRIMARY_REGION --target $CLIENT_INSTANCE"
echo ""
echo "Then: bash -l && cd /opt/kafka"
echo "  ./run_load.sh --mode producer      # pane 1"
echo "  ./run_load.sh --mode consumer      # pane 2"
echo "  ./watch_failover.sh                # pane 3"
echo "  ./simulate_primary_failure.sh      # pane 4 (triggers failover)"
echo "  ./failback.sh                      # (restores primary)"
echo ""
echo "IMPORTANT: Destroy when done — see ./destroy.sh or README 'Clean up'."
echo "  The ARC cluster bills hourly even when idle."
