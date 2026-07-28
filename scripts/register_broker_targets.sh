#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# register_broker_targets.sh
#
# After the MSK cluster is ACTIVE, run this script to discover the broker IPs and
# register them as targets in the NLB target group. This is the post-deploy step
# that wires the bootstrap NLB to the live brokers.
#
# Run it from the EC2 client instance (via SSM Session Manager) so that the broker
# DNS names resolve to in-VPC private IPs. Running it from outside the VPC will
# resolve to nothing (or the wrong addresses).
#
# Usage:
#   ./register_broker_targets.sh \
#       --cluster-arn      <MSK_CLUSTER_ARN> \
#       --target-group-arn <TARGET_GROUP_ARN> \
#       [--region          <AWS_REGION>] \
#       [--stack-name      <CFN_STACK_NAME>]
#
# If --cluster-arn / --target-group-arn are omitted, the script reads them from the
# CloudFormation stack outputs (default stack name: MskCustomDomainStack).
# If --region is omitted, it is taken from the instance metadata / AWS config.

set -euo pipefail

CLUSTER_ARN=""
TARGET_GROUP_ARN=""
REGION=""
STACK_NAME=""
CLUSTER="primary"   # primary | dr  (selects which per-region stack to read)
PORT=9098

# Cross-region: each cluster is its own stack in its own region.
PRIMARY_CLUSTER_STACK="${PRIMARY_CLUSTER_STACK:-MskPrimaryCluster}"
DR_CLUSTER_STACK="${DR_CLUSTER_STACK:-MskDrCluster}"
PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
DR_REGION="${DR_REGION:-us-west-2}"

usage() {
    cat >&2 <<EOF
Usage: $0 [--cluster primary|dr] [--cluster-arn <ARN>] [--target-group-arn <ARN>] \\
          [--region <REGION>] [--stack-name <NAME>]

This cross-region DR demo has TWO clusters in TWO regions. Register both:
    $0 --cluster primary    # reads ${PRIMARY_CLUSTER_STACK} in ${PRIMARY_REGION}
    $0 --cluster dr         # reads ${DR_CLUSTER_STACK} in ${DR_REGION}

Both can be run from the single client instance: broker DNS resolves to private
IPs that the client reaches over the Transit Gateway, and the ELB register call
targets the cluster's own region. Explicit --cluster-arn / --target-group-arn /
--region / --stack-name override the per-cluster defaults.
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cluster) CLUSTER="$2"; shift 2 ;;
        --cluster-arn) CLUSTER_ARN="$2"; shift 2 ;;
        --target-group-arn) TARGET_GROUP_ARN="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --stack-name) STACK_NAME="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

# Each cluster stack exposes its ARN + target group under the SAME output keys
# (ClusterArn / TargetGroupArn); the stack + region differ by role.
CLUSTER_OUTPUT_KEY="ClusterArn"
TG_OUTPUT_KEY="TargetGroupArn"
case "$CLUSTER" in
    primary)
        [[ -z "$STACK_NAME" ]] && STACK_NAME="$PRIMARY_CLUSTER_STACK"
        [[ -z "$REGION" ]] && REGION="$PRIMARY_REGION" ;;
    dr)
        [[ -z "$STACK_NAME" ]] && STACK_NAME="$DR_CLUSTER_STACK"
        [[ -z "$REGION" ]] && REGION="$DR_REGION" ;;
    *) echo "ERROR: --cluster must be 'primary' or 'dr'" >&2; usage ;;
esac
echo "Cluster role: $CLUSTER"

# ---------------------------------------------------------------------------
# Resolve region (flag > env > instance metadata IMDSv2 > aws config default)
# ---------------------------------------------------------------------------
if [[ -z "$REGION" ]]; then
    REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
fi
if [[ -z "$REGION" ]]; then
    TOKEN=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
    if [[ -n "$TOKEN" ]]; then
        REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
            "http://169.254.169.254/latest/dynamic/instance-identity/document" 2>/dev/null \
            | grep -oP '"region"\s*:\s*"\K[^"]+' || true)
    fi
fi
if [[ -z "$REGION" ]]; then
    echo "ERROR: Could not determine AWS region. Pass --region <REGION>." >&2
    exit 1
fi
echo "Region: $REGION"

# ---------------------------------------------------------------------------
# Resolve cluster ARN / target group ARN from stack outputs if not supplied
# ---------------------------------------------------------------------------
get_output() {
    aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
        --output text 2>/dev/null
}

if [[ -z "$CLUSTER_ARN" ]]; then
    echo "Resolving $CLUSTER_OUTPUT_KEY from stack '$STACK_NAME'..."
    CLUSTER_ARN=$(get_output "$CLUSTER_OUTPUT_KEY")
fi
if [[ -z "$TARGET_GROUP_ARN" ]]; then
    echo "Resolving $TG_OUTPUT_KEY from stack '$STACK_NAME'..."
    TARGET_GROUP_ARN=$(get_output "$TG_OUTPUT_KEY")
fi

if [[ -z "$CLUSTER_ARN" || "$CLUSTER_ARN" == "None" ]]; then
    echo "ERROR: cluster ARN not found. Pass --cluster-arn or check --stack-name." >&2
    exit 1
fi
if [[ -z "$TARGET_GROUP_ARN" || "$TARGET_GROUP_ARN" == "None" ]]; then
    echo "ERROR: target group ARN not found. Pass --target-group-arn or check --stack-name." >&2
    exit 1
fi

echo "Cluster ARN:      $CLUSTER_ARN"
echo "Target group ARN: $TARGET_GROUP_ARN"

# ---------------------------------------------------------------------------
# Discover the IAM bootstrap broker endpoints
# ---------------------------------------------------------------------------
echo ""
echo "Discovering MSK broker endpoints..."
BOOTSTRAP_BROKERS=$(aws kafka get-bootstrap-brokers \
    --cluster-arn "$CLUSTER_ARN" \
    --region "$REGION" \
    --query 'BootstrapBrokerStringSaslIam' \
    --output text)

if [[ -z "$BOOTSTRAP_BROKERS" || "$BOOTSTRAP_BROKERS" == "None" ]]; then
    echo "ERROR: No SASL/IAM bootstrap brokers returned. Is the cluster ACTIVE with IAM auth enabled?" >&2
    exit 1
fi
echo "Bootstrap brokers: $BOOTSTRAP_BROKERS"

# ---------------------------------------------------------------------------
# Resolve each broker hostname to its private IP(s) and build the target list.
# A broker hostname normally resolves to a single private IP, but we handle
# multiple A records defensively and de-duplicate.
# ---------------------------------------------------------------------------
declare -A SEEN_IPS=()
TARGET_ARGS=()

IFS=',' read -ra BROKERS <<< "$BOOTSTRAP_BROKERS"
for broker in "${BROKERS[@]}"; do
    hostname="${broker%%:*}"        # strip ":9098"
    [[ -z "$hostname" ]] && continue
    echo "  Resolving $hostname..."

    # Prefer dig; fall back to getent if bind-utils isn't present.
    ips=$(dig +short "$hostname" A 2>/dev/null || true)
    if [[ -z "$ips" ]]; then
        ips=$(getent ahostsv4 "$hostname" | awk '{print $1}' | sort -u || true)
    fi

    if [[ -z "$ips" ]]; then
        echo "    WARNING: could not resolve $hostname"
        continue
    fi

    while read -r ip; do
        [[ -z "$ip" ]] && continue
        if [[ -z "${SEEN_IPS[$ip]:-}" ]]; then
            SEEN_IPS[$ip]=1
            echo "    -> $ip"
            TARGET_ARGS+=("Id=$ip,Port=$PORT")
        fi
    done <<< "$ips"
done

if [[ ${#TARGET_ARGS[@]} -eq 0 ]]; then
    echo "ERROR: No broker IPs resolved. Run this from within the VPC (e.g. the EC2 client via SSM)." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Register the resolved IPs with the NLB target group
# ---------------------------------------------------------------------------
echo ""
echo "Registering ${#TARGET_ARGS[@]} target(s) with the NLB target group..."
aws elbv2 register-targets \
    --target-group-arn "$TARGET_GROUP_ARN" \
    --region "$REGION" \
    --targets "${TARGET_ARGS[@]}"

echo "Done. Broker IPs registered as NLB targets."
echo ""
echo "Check target health (allow a minute for health checks to pass):"
echo "  aws elbv2 describe-target-health --target-group-arn $TARGET_GROUP_ARN --region $REGION"
echo ""
echo "Test connectivity via the custom domain:"
echo "  kafka-topics.sh --list --command-config /opt/kafka/client-iam.properties --bootstrap-server bootstrap.<your-domain>:$PORT"
