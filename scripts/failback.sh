#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# failback.sh — restore the PRIMARY cluster after a simulated failure.
#
# Reverses the three levers that simulate_primary_failure.sh applied:
#   1. Re-add the broker SG's tcp/9098 ingress rules (from env, deterministically)
#   2. Remove the NACL DENY tcp/9098 entry from the PRIMARY MSK subnets
#   3. ARC routing control primary -> On   (deterministic return to PRIMARY)
#
# Once the primary NLB targets are healthy again and the routing control is On,
# bootstrap.<domain> resolves back to the PRIMARY NLB. If ARC is not wired, the
# CloudWatch alarm path still restores PRIMARY once the NLB is healthy (we nudge
# the alarm to OK to speed that up).

set -uo pipefail

SG="${PRIMARY_BROKER_SG:-}"
REGION="${PRIMARY_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
# CIDRs the broker SG admits on 9098 (defaults match the demo topology).
MSK_CIDR="${PRIMARY_MSK_CIDR:-10.0.0.0/16}"
CLIENT_CIDR="${CLIENT_CIDR:-10.2.0.0/16}"
ALARM="${PRIMARY_HEALTH_ALARM:-}"
RC_ARN="${PRIMARY_ROUTING_CONTROL_ARN:-}"
ARC_CLUSTER_ARN="${ARC_CLUSTER_ARN:-}"
NACL_RULE="${NACL_DENY_RULE_NUMBER:-90}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sg) SG="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --routing-control-arn) RC_ARN="$2"; shift 2 ;;
        --cluster-arn) ARC_CLUSTER_ARN="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$REGION" ]]; then
    TOKEN=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
    REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
        "http://169.254.169.254/latest/dynamic/instance-identity/document" 2>/dev/null \
        | grep -oP '"region"\s*:\s*"\K[^"]+' || true)
fi

if [[ -z "$SG" || -z "$REGION" ]]; then
    echo "ERROR: need PRIMARY_BROKER_SG and region. Pass --sg / --region." >&2
    exit 1
fi

echo "Restoring PRIMARY ($REGION)..."

# ── Lever 3 reverse: re-add the broker SG's tcp/9098 ingress ───────────────
echo "[1/3] Re-adding tcp/9098 ingress on broker SG $SG"
for cidr in "$MSK_CIDR" "$CLIENT_CIDR"; do
    if aws ec2 authorize-security-group-ingress \
        --group-id "$SG" --region "$REGION" \
        --ip-permissions "IpProtocol=tcp,FromPort=9098,ToPort=9098,IpRanges=[{CidrIp=${cidr}}]" \
        >/dev/null 2>&1; then
        echo "  restored 9098 <- ${cidr}"
    else
        echo "  9098 <- ${cidr} already present"
    fi
done
rm -f "/tmp/primary_sg_9098_rules.$(id -u).json" 2>/dev/null || true

# ── Lever 2 reverse: remove the NACL DENY entry ────────────────────────────
echo "[2/3] Removing NACL DENY tcp/9098 from PRIMARY MSK subnets"
VPC=$(aws ec2 describe-security-groups --group-ids "$SG" --region "$REGION" \
    --query "SecurityGroups[0].VpcId" --output text 2>/dev/null || true)
if [[ -z "$VPC" || "$VPC" == "None" ]]; then
    echo "  WARN: could not resolve VPC for SG $SG; skipping NACL cleanup." >&2
else
    NACLS=$(aws ec2 describe-network-acls --region "$REGION" \
        --filters "Name=vpc-id,Values=$VPC" \
        --query "NetworkAcls[].NetworkAclId" --output text 2>/dev/null || true)
    for nacl in $NACLS; do
        if aws ec2 delete-network-acl-entry --region "$REGION" \
            --network-acl-id "$nacl" --rule-number "$NACL_RULE" --ingress \
            >/dev/null 2>&1; then
            echo "  removed DENY rule $NACL_RULE from $nacl"
        else
            echo "  no DENY rule $NACL_RULE on $nacl (already clean)"
        fi
    done
fi

# ── Lever 1 reverse: ARC routing control primary -> On ─────────────────────
echo "[3/3] ARC routing control primary -> On"
if [[ -z "$RC_ARN" || -z "$ARC_CLUSTER_ARN" ]]; then
    echo "  (ARC not wired — skipping routing-control flip)"
else
    endpoints=$(aws route53-recovery-control-config describe-cluster \
        --cluster-arn "$ARC_CLUSTER_ARN" --region us-west-2 \
        --query "Cluster.ClusterEndpoints[].[Region,Endpoint]" --output text 2>/dev/null || true)
    flipped=0
    while read -r ep_region ep_url; do
        [[ -z "$ep_region" ]] && continue
        if aws route53-recovery-cluster update-routing-control-state \
            --routing-control-arn "$RC_ARN" \
            --routing-control-state On \
            --region "$ep_region" --endpoint-url "$ep_url" >/dev/null 2>&1; then
            echo "  ARC routing control -> On (via $ep_region)"; flipped=1; break
        fi
    done <<< "$endpoints"
    [[ "$flipped" == "0" ]] && echo "  WARN: could not flip routing control On; check ARC cluster." >&2
fi

# Discover the health alarm name from the DNS stack if not provided.
if [[ -z "$ALARM" ]]; then
    ALARM=$(aws cloudformation describe-stacks --region "$REGION" \
        --stack-name "${DNS_STACK_NAME:-MskDnsFailover}" \
        --query "Stacks[0].Outputs[?OutputKey=='PrimaryHealthAlarmName'].OutputValue" \
        --output text 2>/dev/null || true)
    [[ "$ALARM" == "None" ]] && ALARM=""
fi

# Optional fast-path for the alarm-based (non-ARC) failover mode: nudge the alarm
# to OK once the primary NLB targets are healthy, so DNS returns to PRIMARY
# promptly instead of waiting out the evaluation window. Best-effort. With ARC
# driving the health check this has no effect on routing (the RECOVERY_CONTROL
# health check is state-based), but it keeps the alarm view tidy.
if [[ -n "$ALARM" ]]; then
    PTG=$(aws cloudformation describe-stacks --region "$REGION" \
        --stack-name "${PRIMARY_CLUSTER_STACK:-MskPrimaryCluster}" \
        --query "Stacks[0].Outputs[?OutputKey=='TargetGroupArn'].OutputValue" \
        --output text 2>/dev/null || true)
    if [[ -n "$PTG" && "$PTG" != "None" ]]; then
        echo -n "  waiting for primary NLB targets to become healthy"
        for _ in $(seq 1 18); do
            healthy=$(aws elbv2 describe-target-health --region "$REGION" \
                --target-group-arn "$PTG" \
                --query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" \
                --output text 2>/dev/null || echo 0)
            [[ "$healthy" -ge 1 ]] && { echo " — healthy"; break; }
            echo -n "."; sleep 10
        done
    fi
    if aws cloudwatch set-alarm-state --region "$REGION" \
        --alarm-name "$ALARM" --state-value OK \
        --state-reason "failback: primary brokers restored" >/dev/null 2>&1; then
        echo "  nudged alarm '$ALARM' -> OK"
    fi
fi

echo ""
echo "PRIMARY restored. bootstrap.<domain> returns to the PRIMARY NLB once the"
echo "routing control is On (ARC mode) or the health check is healthy (alarm mode)."
echo "Watch with: ./watch_failover.sh"
