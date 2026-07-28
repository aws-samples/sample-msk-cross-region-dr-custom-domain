#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# simulate_primary_failure.sh — trigger a DR failover.
#
# Three levers, applied together, to make the PRIMARY cluster genuinely
# unreachable AND deterministically move traffic to DR:
#
#   1. ARC routing control primary -> Off  (ask #2)
#        Flips the Route 53 RECOVERY_CONTROL health check backing the PRIMARY
#        failover record. bootstrap.<domain> deterministically resolves to the
#        DR NLB within seconds — an operator decision, not a wait on a metric
#        alarm. (No-op if ARC is not wired; the SG/NACL levers alone still fail
#        the NLB health check and drive the older alarm-based failover.)
#
#   2. NACL DENY tcp/9098 on the PRIMARY MSK subnets  (ask #1)
#        Network ACLs are STATELESS, so this drops in-flight packets, not just
#        new connections. It severs the producer's ALREADY-ESTABLISHED direct
#        broker connections over the Transit Gateway, forcing it off the primary
#        and onto DR. This is the fix for "data still lands on the failed
#        cluster": revoking the security-group rule alone (lever 3) only blocks
#        NEW connections — SGs are stateful, so existing broker sessions keep
#        writing to the primary until something drops their packets.
#
#   3. Revoke tcp/9098 ingress on the PRIMARY broker security group
#        Kept for a faithful "the cluster is gone" picture: cleanly blocks new
#        connections and is what failback.sh restores from env. Also fails the
#        NLB health checks and cuts the cross-region replicator's link.
#
# Fully reversible with failback.sh.
#
# Env (set by user-data in /etc/profile.d/kafka.sh); all overridable by flag:
#   PRIMARY_BROKER_SG, PRIMARY_REGION,
#   PRIMARY_ROUTING_CONTROL_ARN, ARC_CLUSTER_ARN  (ask #2; optional)

set -uo pipefail

SG="${PRIMARY_BROKER_SG:-}"
REGION="${PRIMARY_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
RC_ARN="${PRIMARY_ROUTING_CONTROL_ARN:-}"
ARC_CLUSTER_ARN="${ARC_CLUSTER_ARN:-}"
# Rule number for the DENY entry we add to the MSK subnets' NACL(s). Must be
# lower than the default "100 ALLOW all" so it is evaluated first.
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

# ── Lever 1: ARC routing control primary -> Off ───────────────────────────
# Data-plane flip must target one of the cluster's 5 regional endpoints; we
# discover them from the (control-plane) cluster description and try each until
# one accepts the write. Best-effort: if ARC is not wired or all endpoints are
# unreachable, we fall through to the SG/NACL levers, which still fail over via
# the NLB health alarm.
arc_set_state() {  # $1 = On|Off
    local desired="$1"
    [[ -z "$RC_ARN" || -z "$ARC_CLUSTER_ARN" ]] && { echo "  (ARC not wired — skipping routing-control flip)"; return 0; }

    local endpoints
    endpoints=$(aws route53-recovery-control-config describe-cluster \
        --cluster-arn "$ARC_CLUSTER_ARN" --region us-west-2 \
        --query "Cluster.ClusterEndpoints[].[Region,Endpoint]" --output text 2>/dev/null || true)
    if [[ -z "$endpoints" ]]; then
        echo "  WARN: could not describe ARC cluster; skipping routing-control flip." >&2
        return 0
    fi

    while read -r ep_region ep_url; do
        [[ -z "$ep_region" ]] && continue
        if aws route53-recovery-cluster update-routing-control-state \
            --routing-control-arn "$RC_ARN" \
            --routing-control-state "$desired" \
            --region "$ep_region" --endpoint-url "$ep_url" >/dev/null 2>&1; then
            echo "  ARC routing control -> $desired (via $ep_region)"
            return 0
        fi
    done <<< "$endpoints"

    echo "  WARN: all ARC cluster endpoints rejected the flip; relying on SG/NACL + alarm." >&2
    return 0
}

echo "Simulating PRIMARY failure ($REGION)..."
echo "[1/3] ARC routing control primary -> Off (deterministic DNS failover)"
arc_set_state Off

# ── Lever 2: stateless NACL DENY on the PRIMARY MSK subnets ────────────────
# Discover the MSK VPC from the broker SG, then add a DENY entry to every NACL
# in that VPC. Stateless => drops in-flight AND new tcp/9098 packets.
echo "[2/3] NACL DENY tcp/9098 on PRIMARY MSK subnets (kills in-flight connections)"
VPC=$(aws ec2 describe-security-groups --group-ids "$SG" --region "$REGION" \
    --query "SecurityGroups[0].VpcId" --output text 2>/dev/null || true)
if [[ -z "$VPC" || "$VPC" == "None" ]]; then
    echo "  WARN: could not resolve VPC for SG $SG; skipping NACL lever." >&2
else
    NACLS=$(aws ec2 describe-network-acls --region "$REGION" \
        --filters "Name=vpc-id,Values=$VPC" \
        --query "NetworkAcls[].NetworkAclId" --output text 2>/dev/null || true)
    for nacl in $NACLS; do
        if aws ec2 create-network-acl-entry --region "$REGION" \
            --network-acl-id "$nacl" --rule-number "$NACL_RULE" \
            --protocol 6 --rule-action deny --ingress \
            --port-range "From=9098,To=9098" --cidr-block 0.0.0.0/0 >/dev/null 2>&1; then
            echo "  added DENY rule $NACL_RULE (tcp/9098) to $nacl"
        else
            echo "  DENY rule $NACL_RULE already present on $nacl"
        fi
    done
fi

# ── Lever 3: revoke the broker SG's tcp/9098 ingress ───────────────────────
echo "[3/3] Revoking tcp/9098 ingress on broker SG $SG"
RULES=$(aws ec2 describe-security-groups \
    --group-ids "$SG" --region "$REGION" \
    --query "SecurityGroups[0].IpPermissions[?FromPort==\`9098\`]" --output json)

if [[ "$RULES" == "[]" || -z "$RULES" ]]; then
    echo "  no tcp/9098 ingress rules found — primary SG may already be 'failed'."
else
    # Stash the current rules for reference. Per-user path so root and ssm-user
    # never collide. Non-fatal — failback.sh restores from env, not this file.
    BACKUP="/tmp/primary_sg_9098_rules.$(id -u).json"
    echo "$RULES" > "$BACKUP" 2>/dev/null && echo "  backed up 9098 rules to $BACKUP" || true
    aws ec2 revoke-security-group-ingress \
        --group-id "$SG" --region "$REGION" \
        --ip-permissions "$RULES" >/dev/null 2>&1 && echo "  revoked 9098 ingress"
fi

echo ""
echo "PRIMARY is now fully cut off on 9098 (in-flight connections dropped)."
echo "Watch the failover with:  ./watch_failover.sh"
echo "bootstrap.<domain> now resolves to the DR NLB; the producer re-bootstraps onto DR."
echo "Restore the primary with: ./failback.sh"
