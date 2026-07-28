#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# enable_source_multivpc.sh — prepare the PRIMARY cluster as a cross-region
# replication SOURCE, AFTER it is ACTIVE and BEFORE deploying MskReplicator.
#
# Why this is a post-deploy step (not CloudFormation):
#   MSK refuses to CREATE a cluster with any vpcConnectivity auth scheme enabled
#   ("all vpcConnectivity auth schemes must be disabled ('enabled': false). You
#   can enable auth schemes after the cluster is created."). And the Replicator
#   resource policy is only meaningful once multi-VPC connectivity is on. So both
#   are applied here, once, against the live ACTIVE cluster.
#
# Steps:
#   1. update-cluster-configuration is NOT used; we use update-connectivity to
#      turn on SASL/IAM multi-VPC private connectivity.
#   2. put-cluster-policy attaches a resource policy letting the MSK Replicator
#      service read from this cluster.
#
# Usage:
#   ./enable_source_multivpc.sh [--cluster-arn <ARN>] [--region <REGION>] [--stack <NAME>]
# Defaults: reads MskPrimaryCluster/ClusterArn output in PRIMARY_REGION.

set -euo pipefail

PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
PRIMARY_CLUSTER_STACK="${PRIMARY_CLUSTER_STACK:-MskPrimaryCluster}"
CLUSTER_ARN=""
REGION="$PRIMARY_REGION"
STACK="$PRIMARY_CLUSTER_STACK"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cluster-arn) CLUSTER_ARN="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --stack) STACK="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$CLUSTER_ARN" ]]; then
    CLUSTER_ARN=$(aws cloudformation describe-stacks --region "$REGION" \
        --stack-name "$STACK" \
        --query "Stacks[0].Outputs[?OutputKey=='ClusterArn'].OutputValue" \
        --output text 2>/dev/null)
fi
if [[ -z "$CLUSTER_ARN" || "$CLUSTER_ARN" == "None" ]]; then
    echo "ERROR: could not resolve source cluster ARN (pass --cluster-arn)." >&2
    exit 1
fi
echo "Source cluster: $CLUSTER_ARN ($REGION)"

# The cluster must be ACTIVE before update-connectivity is accepted.
state=$(aws kafka describe-cluster-v2 --region "$REGION" --cluster-arn "$CLUSTER_ARN" \
    --query 'ClusterInfo.State' --output text 2>/dev/null || echo "?")
echo "Cluster state: $state"
if [[ "$state" != "ACTIVE" ]]; then
    echo "ERROR: cluster is not ACTIVE yet. Wait, then re-run." >&2
    exit 1
fi

# Current version is required for update operations.
current_version=$(aws kafka describe-cluster-v2 --region "$REGION" --cluster-arn "$CLUSTER_ARN" \
    --query 'ClusterInfo.CurrentVersion' --output text)
echo "Current version: $current_version"

echo "Enabling SASL/IAM multi-VPC private connectivity..."
aws kafka update-connectivity --region "$REGION" \
    --cluster-arn "$CLUSTER_ARN" \
    --current-version "$current_version" \
    --connectivity-info '{"VpcConnectivity":{"ClientAuthentication":{"Sasl":{"Iam":{"Enabled":true}}}}}' \
    >/dev/null
echo "update-connectivity submitted (cluster will go to UPDATING, then ACTIVE)."

echo "Attaching the MSK Replicator resource policy..."
ACCT="${CLUSTER_ARN#arn:aws:kafka:*:}"; ACCT="${ACCT%%:*}"
aws kafka put-cluster-policy --region "$REGION" \
    --cluster-arn "$CLUSTER_ARN" \
    --policy "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [{
            \"Effect\": \"Allow\",
            \"Principal\": {\"Service\": \"kafka.amazonaws.com\"},
            \"Action\": [
                \"kafka:CreateVpcConnection\",
                \"kafka:GetBootstrapBrokers\",
                \"kafka:DescribeCluster\",
                \"kafka:DescribeClusterV2\"
            ],
            \"Resource\": \"${CLUSTER_ARN}\"
        }]
    }" >/dev/null
echo "put-cluster-policy done."

echo ""
echo "Done. Wait for the cluster to return to ACTIVE:"
echo "  aws kafka describe-cluster-v2 --region $REGION --cluster-arn $CLUSTER_ARN --query 'ClusterInfo.State'"
echo "Then deploy the replicator:  cdk deploy MskReplicator"
