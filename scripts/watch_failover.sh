#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# watch_failover.sh — live dashboard of the CROSS-REGION DR failover state.
#
# Refreshes every few seconds and shows:
#   - the CloudWatch alarm state (primary NLB health, primary region)
#   - the Route 53 health-check status
#   - which region's NLB bootstrap.<domain> currently resolves to (PRIMARY vs DR)
#   - the resolved IP(s)
#
# Cross-region specifics:
#   - The alarm + Route 53 health check live in the DNS stack in PRIMARY_REGION.
#   - The primary NLB is in PRIMARY_REGION; the DR NLB is in DR_REGION.
#   - The alarm name + health-check id are discovered from the DNS stack at
#     runtime (they are not baked into env, to avoid a circular stack dep).
#
# Run this in its own SSM pane while you trigger simulate_primary_failure.sh in
# another. It visualizes the cross-region cutover so the room can see DR is real.

set -uo pipefail

PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
DR_REGION="${DR_REGION:-us-west-2}"
INTERVAL="${WATCH_INTERVAL:-5}"
DNS_STACK="${DNS_STACK_NAME:-MskDnsFailover}"
PRIMARY_CLUSTER_STACK="${PRIMARY_CLUSTER_STACK:-MskPrimaryCluster}"
DR_CLUSTER_STACK="${DR_CLUSTER_STACK:-MskDrCluster}"
RC_ARN="${PRIMARY_ROUTING_CONTROL_ARN:-}"
ARC_CLUSTER_ARN="${ARC_CLUSTER_ARN:-}"

BOOTSTRAP="${BOOTSTRAP_DOMAIN:-bootstrap.example.internal:9098}"
HOST="${BOOTSTRAP%%:*}"

# Read a single CloudFormation stack output value by key, from a given region.
stack_output() {  # $1 = stack, $2 = output key, $3 = region
    aws cloudformation describe-stacks --region "$3" \
        --stack-name "$1" \
        --query "Stacks[0].Outputs[?OutputKey=='${2}'].OutputValue" \
        --output text 2>/dev/null || true
}

nlb_ips() {  # $1 = nlb dns name
    dig +short "$1" 2>/dev/null | sort -u | tr '\n' ' '
}

# Discover the alarm name + health-check id from the DNS stack (primary region),
# and each NLB DNS from its cluster stack (in its own region).
ALARM="${PRIMARY_HEALTH_ALARM:-$(stack_output "$DNS_STACK" PrimaryHealthAlarmName "$PRIMARY_REGION")}"
HC_ID="${FAILOVER_HEALTHCHECK_ID:-$(stack_output "$DNS_STACK" FailoverHealthCheckId "$PRIMARY_REGION")}"
PRIMARY_NLB_DNS=$(stack_output "$PRIMARY_CLUSTER_STACK" NlbDns "$PRIMARY_REGION")
DR_NLB_DNS=$(stack_output "$DR_CLUSTER_STACK" NlbDns "$DR_REGION")

if [[ -z "$ALARM" || -z "$HC_ID" ]]; then
    echo "ERROR: could not resolve alarm/health-check from stack '$DNS_STACK' in $PRIMARY_REGION." >&2
    echo "       Set PRIMARY_HEALTH_ALARM / FAILOVER_HEALTHCHECK_ID, or check --stack outputs." >&2
    exit 1
fi

while true; do
    alarm_state=$(aws cloudwatch describe-alarms --region "$PRIMARY_REGION" \
        --alarm-names "$ALARM" \
        --query 'MetricAlarms[0].StateValue' --output text 2>/dev/null || echo "?")
    hc_status=$(aws route53 get-health-check-status --health-check-id "$HC_ID" \
        --query 'HealthCheckObservations[0].StatusReport.Status' \
        --output text 2>/dev/null || echo "n/a (routing-control health check)")

    # ARC routing-control state (only when wired). Read via any live cluster
    # endpoint; the control-plane describe-cluster gives us the endpoint list.
    rc_state=""
    if [[ -n "$RC_ARN" && -n "$ARC_CLUSTER_ARN" ]]; then
        rc_state="?"
        while read -r ep_region ep_url; do
            [[ -z "$ep_region" ]] && continue
            s=$(aws route53-recovery-cluster get-routing-control-state \
                --routing-control-arn "$RC_ARN" \
                --region "$ep_region" --endpoint-url "$ep_url" \
                --query 'RoutingControlState' --output text 2>/dev/null || true)
            [[ -n "$s" ]] && { rc_state="$s"; break; }
        done < <(aws route53-recovery-control-config describe-cluster \
                    --cluster-arn "$ARC_CLUSTER_ARN" --region us-west-2 \
                    --query "Cluster.ClusterEndpoints[].[Region,Endpoint]" --output text 2>/dev/null)
    fi

    current_ips=$(dig +short "$HOST" 2>/dev/null | sort -u | tr '\n' ' ')
    target="UNKNOWN"
    if [[ -n "${PRIMARY_NLB_DNS:-}" ]] && [[ "$current_ips" == "$(nlb_ips "$PRIMARY_NLB_DNS")" ]]; then
        target="PRIMARY ($PRIMARY_REGION)"
    elif [[ -n "${DR_NLB_DNS:-}" ]] && [[ "$current_ips" == "$(nlb_ips "$DR_NLB_DNS")" ]]; then
        target="DR ($DR_REGION)  <<< FAILED OVER"
    fi

    clear
    echo "========== CROSS-REGION DR FAILOVER WATCH ($(date -u +%H:%M:%SZ)) =========="
    [[ -n "$rc_state" ]] && \
    printf "  %-28s %s\n" "ARC routing control:" "primary=$rc_state (On=serve PRIMARY, Off=failover)"
    printf "  %-28s %s\n" "Primary NLB alarm:" "$alarm_state ($PRIMARY_REGION)"
    printf "  %-28s %s\n" "Route53 health check:" "$hc_status"
    printf "  %-28s %s\n" "bootstrap.<domain> ->" "$target"
    printf "  %-28s %s\n" "resolved IP(s):" "${current_ips:-<none>}"
    echo "------------------------------------------------------------------"
    echo "  (alarm OK + check Healthy + PRIMARY = steady state)"
    echo "  (alarm ALARM + check Unhealthy + DR  = failed over to $DR_REGION)"
    echo "  refresh ${INTERVAL}s — Ctrl-C to exit"
    sleep "$INTERVAL"
done
