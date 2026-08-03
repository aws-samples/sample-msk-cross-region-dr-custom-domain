#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Amazon MSK cross-region disaster recovery with a custom domain.

Topology (single account):
  - PRIMARY MSK cluster + bootstrap NLB in us-east-1  (10.0.0.0/16)
  - DR      MSK cluster + bootstrap NLB in us-west-2  (10.1.0.0/16)
  - CLIENT  VPC + SSM EC2 client        in us-east-1  (10.2.0.0/16), separate VPC
  - Transit Gateway in each region, peered cross-region, so the client reaches
    BOTH clusters' brokers directly after bootstrapping through the NLB.
  - Route 53 private zone on bootstrap.<domain> with a failover record pair that
    flips the name from the primary NLB to the DR NLB when an operator turns the
    ARC `primary` routing control Off.
  - Cross-region MSK Replicator (primary -> DR), created in the DR region.

Deploy order matters (see README). app.py only defines the stacks + wiring;
cross-region references are resolved via crossRegionReferences=True.
"""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from cdk.msk_cluster_stack import MskClusterStack
from cdk.client_stack import ClientStack
from cdk.network_stack import (
    TransitGatewayStack,
    TgwPeeringStack,
    TgwPeeringAccepterStack,
)
from cdk.dns_failover_stack import DnsFailoverStack
from cdk.replicator_stack import ReplicatorStack
from cdk.routing_control_stack import RoutingControlStack

app = cdk.App()

# ── Context / configuration ───────────────────────────────────────────────
domain_name = app.node.try_get_context("domain_name") or "example.internal"
cluster_name = app.node.try_get_context("cluster_name") or "msk-xregion-dr-demo"
account = os.environ.get("CDK_DEFAULT_ACCOUNT")

PRIMARY_REGION = app.node.try_get_context("primary_region") or "us-east-1"
DR_REGION = app.node.try_get_context("dr_region") or "us-west-2"

# ACM certificate ARNs for the TLS-terminating bootstrap NLBs, one per region.
# Generated + imported by scripts/gen_bootstrap_cert.sh and passed in by
# deploy.sh as `-c primary_cert_arn=... -c dr_cert_arn=...`. When absent (bare
# `cdk synth` / unit tests), the NLB listeners fall back to TCP passthrough so
# the app still synthesizes without a pre-imported certificate.
primary_cert_arn = app.node.try_get_context("primary_cert_arn")
dr_cert_arn = app.node.try_get_context("dr_cert_arn")

# Non-overlapping CIDRs (mandatory for TGW routing).
PRIMARY_MSK_CIDR = "10.0.0.0/16"
DR_MSK_CIDR = "10.1.0.0/16"
CLIENT_CIDR = "10.2.0.0/16"

primary_env = cdk.Environment(account=account, region=PRIMARY_REGION)
dr_env = cdk.Environment(account=account, region=DR_REGION)

# ── MSK cluster stacks (one per region) ───────────────────────────────────
primary_cluster = MskClusterStack(
    app, "MskPrimaryCluster",
    env=primary_env,
    cluster_name=f"{cluster_name}-primary",
    vpc_cidr=PRIMARY_MSK_CIDR,
    client_vpc_cidr=CLIENT_CIDR,
    role="primary",
    is_replication_source=True,   # cross-region source: multi-VPC + resource policy
    certificate_arn=primary_cert_arn,
    cross_region_references=True,
    description="PRIMARY MSK Express cluster + bootstrap NLB (us-east-1)",
)

dr_cluster = MskClusterStack(
    app, "MskDrCluster",
    env=dr_env,
    cluster_name=f"{cluster_name}-dr",
    vpc_cidr=DR_MSK_CIDR,
    client_vpc_cidr=CLIENT_CIDR,
    role="dr",
    is_replication_source=False,
    certificate_arn=dr_cert_arn,
    cross_region_references=True,
    description="DR MSK Express cluster + bootstrap NLB (us-west-2)",
)

# ── ARC routing controls (config plane lives in us-west-2) ────────────────
# The operator-controlled failover switch, and the only thing that drives the
# Route 53 failover record. Deployed in the DR region because ARC's
# recovery-control CONFIG plane is us-west-2. NOTE: the ARC cluster carries an
# hourly cost for as long as it exists — see the README "Cleanup" section.
routing_control = RoutingControlStack(
    app, "MskRoutingControls",
    env=dr_env,
    cluster_name=cluster_name,
    cross_region_references=True,
    description="ARC cluster + routing controls for operator-driven failover (us-west-2)",
)
primary_rc_arn = routing_control.primary_routing_control_arn
arc_cluster_arn = routing_control.cluster_arn

# ── Client stack (separate VPC, primary region) ───────────────────────────
client = ClientStack(
    app, "MskClient",
    env=primary_env,
    vpc_cidr=CLIENT_CIDR,
    domain_name=domain_name,
    primary_region=PRIMARY_REGION,
    dr_region=DR_REGION,
    primary_cluster_arn=primary_cluster.cluster_construct.cluster_arn,
    dr_cluster_arn=dr_cluster.cluster_construct.cluster_arn,
    primary_broker_sg_id=primary_cluster.cluster_construct.broker_sg.security_group_id,
    primary_msk_cidr=PRIMARY_MSK_CIDR,
    primary_routing_control_arn=primary_rc_arn,
    arc_cluster_arn=arc_cluster_arn,
    cross_region_references=True,
    description="Separate client VPC + SSM Kafka client (us-east-1)",
)

# ── Transit Gateways (one per region) + cross-region peering ──────────────
tgw_primary = TransitGatewayStack(
    app, "TgwPrimary",
    env=primary_env,
    role="primary",
    attachments=[
        # Client must reach BOTH MSK VPCs: primary (same-region, 10.0) AND DR
        # (cross-region, 10.1). The same-region route is NOT implicit — the
        # client and primary MSK are separate VPCs joined only by the TGW.
        ("ClientVpc", client.vpc,
         [s.subnet_id for s in client.vpc.private_subnets],
         [PRIMARY_MSK_CIDR, DR_MSK_CIDR]),
        # Primary MSK must reach the client (10.2) for the direct broker path,
        # and the DR MSK CIDR (10.1) for the replication network path.
        ("PrimaryMskVpc", primary_cluster.vpc,
         [s.subnet_id for s in primary_cluster.vpc.private_subnets],
         [CLIENT_CIDR, DR_MSK_CIDR]),
    ],
    cross_region_references=True,
    description="Transit Gateway (us-east-1): client + primary MSK VPCs",
)

tgw_dr = TransitGatewayStack(
    app, "TgwDr",
    env=dr_env,
    role="dr",
    attachments=[
        # DR MSK must reach the client (10.2) for the post-failover direct broker
        # path, and the primary MSK CIDR (10.0) for the replication network path.
        ("DrMskVpc", dr_cluster.vpc,
         [s.subnet_id for s in dr_cluster.vpc.private_subnets],
         [CLIENT_CIDR, PRIMARY_MSK_CIDR]),
    ],
    cross_region_references=True,
    description="Transit Gateway (us-west-2): DR MSK VPC",
)

# Peering is INITIATED on the primary side, ACCEPTED on the DR side.
tgw_peering = TgwPeeringStack(
    app, "TgwPeering",
    env=primary_env,
    local_tgw_id=tgw_primary.tgw.ref,
    peer_tgw_id=tgw_dr.tgw.ref,
    peer_region=DR_REGION,
    peer_account=account,
    remote_cidrs=[DR_MSK_CIDR],
    cross_region_references=True,
    description="Cross-region TGW peering (initiator, us-east-1)",
)
tgw_peering.add_dependency(tgw_primary)
tgw_peering.add_dependency(tgw_dr)

tgw_accept = TgwPeeringAccepterStack(
    app, "TgwPeeringAccepter",
    env=dr_env,
    peering_attachment_id=tgw_peering.peering.attr_transit_gateway_attachment_id,
    cross_region_references=True,
    description="Cross-region TGW peering (accepter, us-west-2)",
)
tgw_accept.add_dependency(tgw_peering)

# ── Global DNS + operator-driven failover (primary region) ────────────────
dns = DnsFailoverStack(
    app, "MskDnsFailover",
    env=primary_env,
    domain_name=domain_name,
    associated_vpcs=[
        (client.vpc.vpc_id, PRIMARY_REGION),
        (primary_cluster.vpc.vpc_id, PRIMARY_REGION),
        (dr_cluster.vpc.vpc_id, DR_REGION),
    ],
    primary_nlb_dns=primary_cluster.cluster_construct.nlb.load_balancer_dns_name,
    primary_nlb_canonical_zone_id=primary_cluster.cluster_construct.nlb.load_balancer_canonical_hosted_zone_id,
    dr_nlb_dns=dr_cluster.cluster_construct.nlb.load_balancer_dns_name,
    dr_nlb_canonical_zone_id=dr_cluster.cluster_construct.nlb.load_balancer_canonical_hosted_zone_id,
    primary_routing_control_arn=primary_rc_arn,
    cross_region_references=True,
    description="Route 53 cross-region failover for bootstrap.<domain> (us-east-1)",
)
dns.add_dependency(primary_cluster)
dns.add_dependency(dr_cluster)
dns.add_dependency(client)
dns.add_dependency(routing_control)

# ── Cross-region Replicator (DR/target region) ────────────────────────────
replicator = ReplicatorStack(
    app, "MskReplicator",
    env=dr_env,
    cluster_name=cluster_name,
    source_cluster_arn=primary_cluster.cluster_construct.cluster_arn,
    target_cluster_arn=dr_cluster.cluster_construct.cluster_arn,
    source_subnet_ids=[s.subnet_id for s in primary_cluster.vpc.private_subnets],
    target_subnet_ids=[s.subnet_id for s in dr_cluster.vpc.private_subnets],
    target_broker_sg_id=dr_cluster.cluster_construct.broker_sg.security_group_id,
    cross_region_references=True,
    description="Cross-region MSK Replicator primary->DR (us-west-2)",
)
replicator.add_dependency(primary_cluster)
replicator.add_dependency(dr_cluster)

# ── cdk-nag ────────────────────────────────────────────────────────────────
if not app.node.try_get_context("skip_nag"):
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
