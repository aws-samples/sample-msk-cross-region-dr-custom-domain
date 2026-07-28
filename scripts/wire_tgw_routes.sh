#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# wire_tgw_routes.sh — add cross-region TGW routes via the peering attachment.
#
# Subnet route tables already point the remote CIDRs at the local TGW (done in
# CDK). The missing piece is the TGW's OWN route table: it must route the remote
# CIDRs to the cross-region PEERING attachment. Those routes can only be created
# once the peering attachment is in the 'available' state, which is why this is a
# post-deploy step rather than part of the CloudFormation template.
#
# Run this once, from anywhere with credentials for the account, after:
#   1. TgwPrimary + TgwDr are deployed
#   2. TgwPeering (initiator) is deployed
#   3. TgwPeeringAccepter has accepted the peering (status 'available')
#
# It is idempotent: existing routes are reported and skipped.

set -euo pipefail

PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
DR_REGION="${DR_REGION:-us-west-2}"
PRIMARY_MSK_CIDR="${PRIMARY_MSK_CIDR:-10.0.0.0/16}"
DR_MSK_CIDR="${DR_MSK_CIDR:-10.1.0.0/16}"
CLIENT_CIDR="${CLIENT_CIDR:-10.2.0.0/16}"

# Stack names (defaults match app.py).
TGW_PRIMARY_STACK="${TGW_PRIMARY_STACK:-TgwPrimary}"
TGW_DR_STACK="${TGW_DR_STACK:-TgwDr}"
PEERING_STACK="${PEERING_STACK:-TgwPeering}"

stack_output() {  # $1 = stack, $2 = key, $3 = region
    aws cloudformation describe-stacks --region "$3" --stack-name "$1" \
        --query "Stacks[0].Outputs[?OutputKey=='${2}'].OutputValue" \
        --output text 2>/dev/null
}

echo "Resolving TGW + peering identifiers from stack outputs..."
TGW_PRIMARY_ID=$(stack_output "$TGW_PRIMARY_STACK" TgwId "$PRIMARY_REGION")
TGW_DR_ID=$(stack_output "$TGW_DR_STACK" TgwId "$DR_REGION")
PEERING_ID=$(stack_output "$PEERING_STACK" PeeringAttachmentId "$PRIMARY_REGION")

if [[ -z "$TGW_PRIMARY_ID" || -z "$TGW_DR_ID" || -z "$PEERING_ID" ]]; then
    echo "ERROR: could not resolve TGW/peering ids. Check the stacks are deployed." >&2
    exit 1
fi
echo "  TGW (primary, $PRIMARY_REGION): $TGW_PRIMARY_ID"
echo "  TGW (dr,      $DR_REGION): $TGW_DR_ID"
echo "  Peering attachment:        $PEERING_ID"

# Find each TGW's default association route table.
default_rtb() {  # $1 = tgw id, $2 = region
    aws ec2 describe-transit-gateways --region "$2" \
        --transit-gateway-ids "$1" \
        --query 'TransitGateways[0].Options.AssociationDefaultRouteTableId' \
        --output text
}

RTB_PRIMARY=$(default_rtb "$TGW_PRIMARY_ID" "$PRIMARY_REGION")
RTB_DR=$(default_rtb "$TGW_DR_ID" "$DR_REGION")
echo "  Primary TGW route table: $RTB_PRIMARY"
echo "  DR TGW route table:      $RTB_DR"

# Wait for the peering attachment to be 'available' on both sides.
echo "Waiting for peering attachment to become available..."
for region in "$PRIMARY_REGION" "$DR_REGION"; do
    for _ in $(seq 1 30); do
        state=$(aws ec2 describe-transit-gateway-peering-attachments --region "$region" \
            --transit-gateway-attachment-ids "$PEERING_ID" \
            --query 'TransitGatewayPeeringAttachments[0].State' --output text 2>/dev/null || echo "?")
        echo "  [$region] peering state: $state"
        [[ "$state" == "available" ]] && break
        sleep 10
    done
done

add_tgw_route() {  # $1 = cidr, $2 = route-table-id, $3 = region
    if aws ec2 search-transit-gateway-routes --region "$3" \
        --transit-gateway-route-table-id "$2" \
        --filters "Name=route-search.exact-match,Values=$1" \
        --query 'Routes[0].DestinationCidrBlock' --output text 2>/dev/null | grep -q "$1"; then
        echo "  route $1 already present in $2 ($3) — skipping"
        return
    fi
    echo "  adding route $1 -> peering ($2, $3)"
    aws ec2 create-transit-gateway-route --region "$3" \
        --transit-gateway-route-table-id "$2" \
        --destination-cidr-block "$1" \
        --transit-gateway-attachment-id "$PEERING_ID" >/dev/null
}

echo "Adding cross-region TGW routes..."
# From the primary TGW, the DR MSK CIDR is reachable via the peering.
add_tgw_route "$DR_MSK_CIDR" "$RTB_PRIMARY" "$PRIMARY_REGION"
# From the DR TGW, the primary MSK CIDR and the client CIDR are reachable via peering.
add_tgw_route "$PRIMARY_MSK_CIDR" "$RTB_DR" "$DR_REGION"
add_tgw_route "$CLIENT_CIDR" "$RTB_DR" "$DR_REGION"

echo ""
echo "Done. Cross-region TGW routing is wired."
echo "The client VPC can now reach the DR cluster's broker IPs over the peering,"
echo "and the DR cluster can reach back to the client + primary CIDRs."
