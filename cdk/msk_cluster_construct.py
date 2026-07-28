# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Per-cluster construct: MSK Express cluster + bootstrap NLB + security group.

Cross-region variant. Each cluster lives in its own region/VPC and the Kafka
client lives in a SEPARATE VPC (reached over Transit Gateway), so the broker
security group must allow port 9098 from the client VPC CIDR in addition to the
local VPC CIDR (NLB bootstrap / health checks / replicator ENIs).

The port-9098 SG rule on the PRIMARY cluster is the "failure lever" the demo
toggles to trigger failover (see scripts/simulate_primary_failure.sh).
"""

from aws_cdk import (
    RemovalPolicy,
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_msk as msk,
    aws_elasticloadbalancingv2 as elbv2,
    aws_logs as logs,
)
from constructs import Construct


class MskClusterConstruct(Construct):
    """An MSK cluster fronted by a bootstrap-only NLB."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        cluster_name: str,
        # CIDRs allowed to reach the brokers on 9098. We use CIDRs rather than an
        # SG reference because the client is in a different VPC (and possibly a
        # different region), so a same-region SG reference is not possible.
        client_vpc_cidr: str,
        # ACM certificate ARN for bootstrap.<domain>. When supplied, the NLB
        # listener terminates TLS with it and re-encrypts to the brokers; when
        # None (bare `cdk synth` / unit tests), the listener falls back to TCP
        # passthrough so the app still synthesizes without a pre-imported cert.
        certificate_arn: str | None = None,
        kafka_version: str = "3.6.0",
        broker_instance_type: str = "express.m7g.large",
        number_of_brokers: int = 3,
    ) -> None:
        super().__init__(scope, construct_id)

        self.cluster_name = cluster_name

        # ── Security group ────────────────────────────────────────────────
        # The port-9098 ingress rules below are what the demo toggles to
        # simulate a cluster failure (see scripts/simulate_primary_failure.sh).
        self.broker_sg = ec2.SecurityGroup(
            self,
            "BrokerSg",
            vpc=vpc,
            description=f"MSK brokers ({cluster_name})",
            allow_all_outbound=True,
        )
        # Local-VPC traffic: NLB bootstrap ENIs + TCP health checks + the
        # cross-region replicator's ENIs all originate inside this VPC's CIDR.
        self.broker_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(9098),
            description="Kafka IAM from local VPC CIDR (NLB bootstrap/health checks + replicator)",
        )
        # Direct client -> broker path from the SEPARATE client VPC, over TGW.
        # This carries produce/consume traffic after the client bootstraps.
        self.broker_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(client_vpc_cidr),
            connection=ec2.Port.tcp(9098),
            description="Kafka IAM (SASL_SSL) from client VPC over Transit Gateway",
        )

        # ── Broker logs ───────────────────────────────────────────────────
        # KMS-encrypted log group. A CloudWatch Logs log group encrypted with a
        # customer-managed key REQUIRES a key policy granting the regional
        # CloudWatch Logs service principal (logs.<region>.amazonaws.com) the
        # encrypt/decrypt actions — without it, log group creation fails with
        # AccessDenied. The EncryptionContext condition scopes that grant to this
        # account's log groups only. See:
        # https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html
        region = Stack.of(self).region
        account = Stack.of(self).account
        log_key = kms.Key(
            self,
            "LogGroupKey",
            description=f"KMS key for {cluster_name} broker log group",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        log_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchLogs",
                principals=[iam.ServicePrincipal(f"logs.{region}.amazonaws.com")],
                actions=[
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:Describe*",
                ],
                resources=["*"],
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn":
                            f"arn:aws:logs:{region}:{account}:log-group:*",
                    }
                },
            )
        )
        log_group = logs.LogGroup(
            self,
            "LogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
            encryption_key=log_key,
        )

        # ── MSK cluster (Express brokers) ─────────────────────────────────
        # Express brokers (express.m7g.*) are KRaft-based and have FULLY MANAGED
        # storage — there is no StorageInfo/EBS volume to provision (and the
        # service rejects it). They are 3-AZ only, so number_of_brokers must be a
        # multiple of 3 and we must supply 3 client subnets across 3 AZs.
        self.cluster = msk.CfnCluster(
            self,
            "Cluster",
            cluster_name=cluster_name,
            kafka_version=kafka_version,
            number_of_broker_nodes=number_of_brokers,
            broker_node_group_info=msk.CfnCluster.BrokerNodeGroupInfoProperty(
                instance_type=broker_instance_type,
                client_subnets=[s.subnet_id for s in vpc.private_subnets],
                security_groups=[self.broker_sg.security_group_id],
                # NO storage_info: Express brokers manage storage automatically.
            ),
            client_authentication=msk.CfnCluster.ClientAuthenticationProperty(
                sasl=msk.CfnCluster.SaslProperty(
                    iam=msk.CfnCluster.IamProperty(enabled=True),
                ),
            ),
            encryption_info=msk.CfnCluster.EncryptionInfoProperty(
                encryption_in_transit=msk.CfnCluster.EncryptionInTransitProperty(
                    client_broker="TLS",
                    in_cluster=True,
                ),
            ),
            enhanced_monitoring="PER_BROKER",
            logging_info=msk.CfnCluster.LoggingInfoProperty(
                broker_logs=msk.CfnCluster.BrokerLogsProperty(
                    cloud_watch_logs=msk.CfnCluster.CloudWatchLogsProperty(
                        enabled=True,
                        log_group=log_group.log_group_name,
                    ),
                ),
            ),
        )

        # ── Bootstrap NLB (TLS-terminating) ───────────────────────────────
        # The NLB fronts ONLY the bootstrap connection: the client makes one TLS
        # connection to bootstrap.<domain>, the NLB terminates it with the ACM
        # certificate and re-encrypts (a fresh TLS session) to the brokers, then
        # returns the brokers' native DNS names. From there the client connects
        # DIRECTLY to the brokers over Transit Gateway, so the NLB is never in the
        # data path. Terminating TLS (rather than TCP passthrough) lets the client
        # keep hostname verification ON: the listener presents a certificate that
        # matches bootstrap.<domain>. See scripts/gen_bootstrap_cert.sh.
        self.nlb = elbv2.NetworkLoadBalancer(
            self,
            "BootstrapNlb",
            vpc=vpc,
            internet_facing=False,
            cross_zone_enabled=True,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )

        # When a certificate is supplied the NLB terminates TLS and re-encrypts to
        # the brokers (target-group protocol TLS). Without one (bare synth/tests),
        # fall back to TCP passthrough so the app still synthesizes.
        tls_enabled = certificate_arn is not None
        tg_protocol = elbv2.Protocol.TLS if tls_enabled else elbv2.Protocol.TCP

        # Broker IPs are registered post-deploy. The health check stays on plain
        # TCP/9098 (a cheap connect probe) in both modes.
        self.target_group = elbv2.NetworkTargetGroup(
            self,
            "BootstrapTargetGroup",
            vpc=vpc,
            port=9098,
            protocol=tg_protocol,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                protocol=elbv2.Protocol.TCP,
                port="9098",
                interval=Duration.seconds(10),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
        )

        if tls_enabled:
            self.nlb.add_listener(
                "BootstrapListener",
                port=9098,
                protocol=elbv2.Protocol.TLS,
                certificates=[elbv2.ListenerCertificate.from_arn(certificate_arn)],
                default_target_groups=[self.target_group],
            )
        else:
            self.nlb.add_listener(
                "BootstrapListener",
                port=9098,
                protocol=elbv2.Protocol.TCP,
                default_target_groups=[self.target_group],
            )

    # Convenience accessors -------------------------------------------------
    @property
    def cluster_arn(self) -> str:
        return self.cluster.attr_arn

    @property
    def nlb_dns_name(self) -> str:
        return self.nlb.load_balancer_dns_name

    @property
    def target_group_arn(self) -> str:
        return self.target_group.target_group_arn
