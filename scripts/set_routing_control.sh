#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# set_routing_control.sh — read or set the ARC `primary` routing control state.
#
# New routing controls default to Off, so after deploying MskRoutingControls you
# MUST set the primary control On for steady state (On = serve PRIMARY). This is
# also the manual lever for a deterministic failover/failback outside the demo
# scripts:  --state Off  fails over to DR;  --state On  returns to PRIMARY.
#
# Flips are served by the ARC CLUSTER's 5 regional endpoints (not the config
# plane), so this discovers the endpoints and tries each until one accepts — the
# whole point of ARC being that the data plane stays available region-independent.
#
# Reads PRIMARY_ROUTING_CONTROL_ARN + ARC_CLUSTER_ARN from the environment (set on
# the EC2 client by user-data). Off the client, pass --routing-control-arn /
# --cluster-arn, or discover them from the stack outputs:
#   RC=$(aws cloudformation describe-stacks --region us-west-2 \
#          --stack-name MskRoutingControls \
#          --query "Stacks[0].Outputs[?OutputKey=='PrimaryRoutingControlArn'].OutputValue" --output text)

set -uo pipefail

RC_ARN="${PRIMARY_ROUTING_CONTROL_ARN:-}"
ARC_CLUSTER_ARN="${ARC_CLUSTER_ARN:-}"
STATE=""   # empty => read-only (print current state)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --state) STATE="$2"; shift 2 ;;
        --routing-control-arn) RC_ARN="$2"; shift 2 ;;
        --cluster-arn) ARC_CLUSTER_ARN="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$RC_ARN" || -z "$ARC_CLUSTER_ARN" ]]; then
    echo "ERROR: need PRIMARY_ROUTING_CONTROL_ARN and ARC_CLUSTER_ARN (env or flags)." >&2
    exit 1
fi
if [[ -n "$STATE" && "$STATE" != "On" && "$STATE" != "Off" ]]; then
    echo "ERROR: --state must be On or Off (got '$STATE')." >&2
    exit 1
fi

# Endpoint list from the (control-plane) cluster description.
endpoints=$(aws route53-recovery-control-config describe-cluster \
    --cluster-arn "$ARC_CLUSTER_ARN" --region us-west-2 \
    --query "Cluster.ClusterEndpoints[].[Region,Endpoint]" --output text 2>/dev/null || true)
if [[ -z "$endpoints" ]]; then
    echo "ERROR: could not describe ARC cluster $ARC_CLUSTER_ARN." >&2
    exit 1
fi

if [[ -z "$STATE" ]]; then
    # Read-only: return the current state via any live endpoint.
    while read -r r u; do
        [[ -z "$r" ]] && continue
        s=$(aws route53-recovery-cluster get-routing-control-state \
            --routing-control-arn "$RC_ARN" \
            --region "$r" --endpoint-url "$u" \
            --query 'RoutingControlState' --output text 2>/dev/null || true)
        [[ -n "$s" ]] && { echo "primary routing control = $s (via $r)"; exit 0; }
    done <<< "$endpoints"
    echo "ERROR: no ARC cluster endpoint answered the read." >&2
    exit 1
fi

# Write: set the requested state via the first endpoint that accepts.
while read -r r u; do
    [[ -z "$r" ]] && continue
    if aws route53-recovery-cluster update-routing-control-state \
        --routing-control-arn "$RC_ARN" \
        --routing-control-state "$STATE" \
        --region "$r" --endpoint-url "$u" >/dev/null 2>&1; then
        echo "primary routing control -> $STATE (via $r)"
        exit 0
    fi
done <<< "$endpoints"

echo "ERROR: no ARC cluster endpoint accepted the state change." >&2
exit 1
