# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the cross-region MSK DR demo.

CloudFormation-template assertions: synthesize each stack and check the expected
resources and key properties. No deploy, no AWS credentials needed.

Run with:  pytest
"""

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from cdk.msk_cluster_stack import MskClusterStack
from cdk.client_stack import ClientStack
from cdk.network_stack import (
    TransitGatewayStack,
    TgwPeeringStack,
    TgwPeeringAccepterStack,
)
from cdk.dns_failover_stack import DnsFailoverStack
from cdk.replicator_stack import ReplicatorStack

ACCOUNT = "123456789012"
PRIMARY_REGION = "us-east-1"
DR_REGION = "us-west-2"
DOMAIN = "example.internal"
CLUSTER = "msk-xregion-test"

PRIMARY_MSK_CIDR = "10.0.0.0/16"
DR_MSK_CIDR = "10.1.0.0/16"
CLIENT_CIDR = "10.2.0.0/16"
CERT_ARN = f"arn:aws:acm:{PRIMARY_REGION}:{ACCOUNT}:certificate/test-cert"
ARC_CLUSTER_ARN = f"arn:aws:route53-recovery-control::{ACCOUNT}:cluster/test-cluster"
ROUTING_CONTROL_ARN = f"{ARC_CLUSTER_ARN}/routingcontrol/test-control"


def _app():
    return cdk.App(context={"skip_nag": True})


def _primary_cluster(app, certificate_arn=CERT_ARN):
    return MskClusterStack(
        app, "MskPrimaryCluster",
        env=cdk.Environment(account=ACCOUNT, region=PRIMARY_REGION),
        cluster_name=f"{CLUSTER}-primary",
        vpc_cidr=PRIMARY_MSK_CIDR,
        client_vpc_cidr=CLIENT_CIDR,
        role="primary",
        is_replication_source=True,
        certificate_arn=certificate_arn,
        cross_region_references=True,
    )


def _dr_cluster(app, certificate_arn=CERT_ARN):
    return MskClusterStack(
        app, "MskDrCluster",
        env=cdk.Environment(account=ACCOUNT, region=DR_REGION),
        cluster_name=f"{CLUSTER}-dr",
        vpc_cidr=DR_MSK_CIDR,
        client_vpc_cidr=CLIENT_CIDR,
        role="dr",
        is_replication_source=False,
        certificate_arn=certificate_arn,
        cross_region_references=True,
    )


def test_each_cluster_stack_has_one_express_cluster_iam_tls():
    app = _app()
    template = Template.from_stack(_primary_cluster(app))
    template.resource_count_is("AWS::MSK::Cluster", 1)
    template.has_resource_properties(
        "AWS::MSK::Cluster",
        {
            "KafkaVersion": "3.6.0",
            "NumberOfBrokerNodes": 3,
            "ClientAuthentication": {"Sasl": {"Iam": {"Enabled": True}}},
            "EncryptionInfo": {
                "EncryptionInTransit": {"ClientBroker": "TLS", "InCluster": True}
            },
        },
    )


def test_primary_source_does_not_set_multivpc_at_create():
    """MSK rejects creating a cluster with vpcConnectivity auth enabled, so the
    source cluster must NOT carry ConnectivityInfo or a ClusterPolicy in the
    template — those are applied post-deploy by enable_source_multivpc.sh."""
    app = _app()
    template = Template.from_stack(_primary_cluster(app))
    template.resource_count_is("AWS::MSK::ClusterPolicy", 0)
    # The cluster is created with no vpcConnectivity auth scheme enabled.
    cluster = list(template.find_resources("AWS::MSK::Cluster").values())[0]
    bng = cluster["Properties"]["BrokerNodeGroupInfo"]
    assert "ConnectivityInfo" not in bng, "source must not enable multi-VPC at create"
    # It still advertises the post-deploy requirement via an output.
    assert "RequiresPostDeployMultiVpc" in template.find_outputs("*")


def test_dr_cluster_is_not_a_source():
    app = _app()
    template = Template.from_stack(_dr_cluster(app))
    template.resource_count_is("AWS::MSK::ClusterPolicy", 0)
    assert "RequiresPostDeployMultiVpc" not in template.find_outputs("*")


def test_cluster_stack_has_internal_nlb_tls_9098():
    """With a certificate ARN, the bootstrap NLB terminates TLS on 9098 and
    re-encrypts to the brokers (target-group protocol TLS)."""
    app = _app()
    template = Template.from_stack(_primary_cluster(app))
    template.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 1)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {"Scheme": "internal", "Type": "network"},
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {"Port": 9098, "Protocol": "TLS", "TargetType": "ip"},
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 9098,
            "Protocol": "TLS",
            "Certificates": [{"CertificateArn": CERT_ARN}],
        },
    )


def test_cluster_stack_nlb_falls_back_to_tcp_without_cert():
    """Bare synth (no certificate) leaves the bootstrap NLB on TCP passthrough so
    the app still synthesizes without a pre-imported certificate."""
    app = _app()
    template = Template.from_stack(_primary_cluster(app, certificate_arn=None))
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {"Port": 9098, "Protocol": "TCP", "TargetType": "ip"},
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"Port": 9098, "Protocol": "TCP"},
    )


def _dns_stack(app):
    return DnsFailoverStack(
        app, "MskDnsFailover",
        env=cdk.Environment(account=ACCOUNT, region=PRIMARY_REGION),
        domain_name=DOMAIN,
        associated_vpcs=[
            ("vpc-client", PRIMARY_REGION),
            ("vpc-primary", PRIMARY_REGION),
            ("vpc-dr", DR_REGION),
        ],
        primary_nlb_dns="primary-nlb.example.com",
        primary_nlb_canonical_zone_id="Z111",
        dr_nlb_dns="dr-nlb.example.com",
        dr_nlb_canonical_zone_id="Z222",
        primary_routing_control_arn=ROUTING_CONTROL_ARN,
        cross_region_references=True,
    )


def test_dns_stack_failover_pair_and_healthcheck():
    app = _app()
    template = Template.from_stack(_dns_stack(app))
    # Private zone associated with three VPCs (cross-region).
    template.has_resource_properties(
        "AWS::Route53::HostedZone",
        {"VPCs": Match.array_with([
            Match.object_like({"VPCRegion": DR_REGION}),
        ])},
    )
    template.has_resource_properties(
        "AWS::Route53::RecordSet",
        {"Name": f"bootstrap.{DOMAIN}.", "Failover": "PRIMARY", "SetIdentifier": "primary"},
    )
    template.has_resource_properties(
        "AWS::Route53::RecordSet",
        {"Name": f"bootstrap.{DOMAIN}.", "Failover": "SECONDARY", "SetIdentifier": "secondary"},
    )
    template.resource_count_is("AWS::Route53::HealthCheck", 1)


def test_dns_failover_is_driven_only_by_the_arc_routing_control():
    """ARC is the only failover driver: the health check is RECOVERY_CONTROL bound
    to the primary routing control, and no CloudWatch alarm is created."""
    app = _app()
    template = Template.from_stack(_dns_stack(app))
    template.has_resource_properties(
        "AWS::Route53::HealthCheck",
        {"HealthCheckConfig": Match.object_like({
            "Type": "RECOVERY_CONTROL",
            "RoutingControlArn": ROUTING_CONTROL_ARN,
        })},
    )
    template.resource_count_is("AWS::CloudWatch::Alarm", 0)


def test_replicator_identical_topic_names_and_offsets():
    app = _app()
    template = Template.from_stack(ReplicatorStack(
        app, "MskReplicator",
        env=cdk.Environment(account=ACCOUNT, region=DR_REGION),
        cluster_name=CLUSTER,
        source_cluster_arn=f"arn:aws:kafka:{PRIMARY_REGION}:{ACCOUNT}:cluster/p/uuid-1",
        target_cluster_arn=f"arn:aws:kafka:{DR_REGION}:{ACCOUNT}:cluster/d/uuid-2",
        source_subnet_ids=["subnet-a", "subnet-b", "subnet-c"],
        target_subnet_ids=["subnet-x", "subnet-y", "subnet-z"],
        target_broker_sg_id="sg-dr",
        cross_region_references=True,
    ))
    template.resource_count_is("AWS::MSK::Replicator", 1)
    template.has_resource_properties(
        "AWS::MSK::Replicator",
        {"ReplicationInfoList": Match.array_with([Match.object_like({
            "TopicReplication": Match.object_like({
                "TopicNameConfiguration": {"Type": "IDENTICAL"},
            }),
            "ConsumerGroupReplication": Match.object_like({
                "SynchroniseConsumerGroupOffsets": True,
            }),
        })])},
    )


def test_tgw_stack_creates_tgw_and_attachments():
    app = _app()
    primary = _primary_cluster(app)
    client = ClientStack(
        app, "MskClient",
        env=cdk.Environment(account=ACCOUNT, region=PRIMARY_REGION),
        vpc_cidr=CLIENT_CIDR,
        domain_name=DOMAIN,
        primary_region=PRIMARY_REGION,
        dr_region=DR_REGION,
        primary_cluster_arn=primary.cluster_construct.cluster_arn,
        dr_cluster_arn=f"arn:aws:kafka:{DR_REGION}:{ACCOUNT}:cluster/d/uuid-2",
        primary_broker_sg_id=primary.cluster_construct.broker_sg.security_group_id,
        primary_msk_cidr=PRIMARY_MSK_CIDR,
        primary_routing_control_arn=ROUTING_CONTROL_ARN,
        arc_cluster_arn=ARC_CLUSTER_ARN,
        cross_region_references=True,
    )
    template = Template.from_stack(TransitGatewayStack(
        app, "TgwPrimary",
        env=cdk.Environment(account=ACCOUNT, region=PRIMARY_REGION),
        role="primary",
        attachments=[
            ("ClientVpc", client.vpc,
             [s.subnet_id for s in client.vpc.private_subnets],
             [PRIMARY_MSK_CIDR, DR_MSK_CIDR]),
            ("PrimaryMskVpc", primary.vpc,
             [s.subnet_id for s in primary.vpc.private_subnets],
             [CLIENT_CIDR, DR_MSK_CIDR]),
        ],
        cross_region_references=True,
    ))
    template.resource_count_is("AWS::EC2::TransitGateway", 1)
    # One attachment per VPC (client + primary MSK).
    template.resource_count_is("AWS::EC2::TransitGatewayAttachment", 2)


def test_peering_accepter_uses_custom_resource():
    app = _app()
    template = Template.from_stack(TgwPeeringAccepterStack(
        app, "TgwPeeringAccepter",
        env=cdk.Environment(account=ACCOUNT, region=DR_REGION),
        peering_attachment_id="tgw-attach-123",
        cross_region_references=True,
    ))
    # AwsCustomResource provisions a Custom:: resource backed by a Lambda.
    template.resource_count_is("AWS::Lambda::Function", 1)


def test_client_stack_one_instance_in_separate_vpc():
    app = _app()
    primary = _primary_cluster(app)
    template = Template.from_stack(ClientStack(
        app, "MskClient",
        env=cdk.Environment(account=ACCOUNT, region=PRIMARY_REGION),
        vpc_cidr=CLIENT_CIDR,
        domain_name=DOMAIN,
        primary_region=PRIMARY_REGION,
        dr_region=DR_REGION,
        primary_cluster_arn=primary.cluster_construct.cluster_arn,
        dr_cluster_arn=f"arn:aws:kafka:{DR_REGION}:{ACCOUNT}:cluster/d/uuid-2",
        primary_broker_sg_id=primary.cluster_construct.broker_sg.security_group_id,
        primary_msk_cidr=PRIMARY_MSK_CIDR,
        primary_routing_control_arn=ROUTING_CONTROL_ARN,
        arc_cluster_arn=ARC_CLUSTER_ARN,
        cross_region_references=True,
    ))
    template.resource_count_is("AWS::EC2::Instance", 1)
    template.resource_count_is("AWS::MSK::Cluster", 0)  # no MSK in the client VPC
