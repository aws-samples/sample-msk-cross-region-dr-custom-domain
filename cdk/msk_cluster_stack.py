# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Per-region MSK cluster stack (deployed once in each region: primary + DR).

Each instance creates:
  - a dedicated MSK VPC (non-overlapping CIDR)
  - an Express MSK cluster with IAM auth + TLS
  - a bootstrap-only NLB + target group

Cross-region specifics handled here:
  - The broker SG admits 9098 from the SEPARATE client VPC CIDR (over TGW).
  - The PRIMARY cluster is the cross-region replication SOURCE, so it must have
    multi-VPC private connectivity turned on and a resource-based policy that
    lets the MSK Replicator service read from it (see msk-replicator-cross-region
    docs). The DR cluster is the target and needs neither.

The NLB/Route53/Replicator wiring lives in separate stacks so this one stays a
clean, reusable "a cluster in a region" unit.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    Tags,
    aws_ec2 as ec2,
)
from constructs import Construct
from cdk_nag import NagSuppressions

from cdk.msk_cluster_construct import MskClusterConstruct


class MskClusterStack(Stack):
    """One MSK Express cluster + bootstrap NLB in a single region."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_name: str,
        vpc_cidr: str,
        client_vpc_cidr: str,
        role: str,  # "primary" | "dr"
        is_replication_source: bool = False,
        # ACM certificate ARN (in this stack's region) for the TLS-terminating
        # bootstrap NLB. None -> NLB falls back to TCP passthrough (bare synth).
        certificate_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.role = role

        # ── MSK VPC (this cluster's own VPC; non-overlapping CIDR) ─────────
        self.vpc = ec2.Vpc(
            self,
            "MskVpc",
            ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
            max_azs=3,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        self.vpc.add_flow_log("FlowLog")

        # ── Cluster + bootstrap NLB ───────────────────────────────────────
        self.cluster_construct = MskClusterConstruct(
            self,
            "Cluster",
            vpc=self.vpc,
            cluster_name=cluster_name,
            client_vpc_cidr=client_vpc_cidr,
            certificate_arn=certificate_arn,
        )

        # ── Cross-region replication SOURCE requirements ──────────────────
        # As the replication source, the primary cluster must have multi-VPC
        # private connectivity (IAM) enabled and a resource policy granting the
        # MSK Replicator service access. MSK only allows enabling VPC connectivity
        # auth AFTER a cluster is created, so both are applied post-deploy by
        # scripts/enable_source_multivpc.sh (run once the cluster is ACTIVE,
        # before deploying the Replicator — see the README deploy order). Here we
        # only surface an output hint that the step is required.
        if is_replication_source:
            CfnOutput(self, "RequiresPostDeployMultiVpc", value="true",
                      description="Run scripts/enable_source_multivpc.sh on this "
                                  "cluster (enables multi-VPC IAM connectivity + "
                                  "Replicator resource policy) before deploying "
                                  "MskReplicator.")

        Tags.of(self).add("Project", "msk-dr-cross-region-custom-domain")
        Tags.of(self).add("Purpose", "DR-failover-demo")
        Tags.of(self).add("ClusterRole", role)

        # ── Outputs (consumed by the DNS/failover + replicator stacks) ────
        CfnOutput(self, "ClusterArn", value=self.cluster_construct.cluster_arn,
                  description=f"{role} MSK cluster ARN",
                  export_name=f"{construct_id}-ClusterArn")
        CfnOutput(self, "NlbDns", value=self.cluster_construct.nlb_dns_name,
                  description=f"{role} bootstrap NLB DNS")
        CfnOutput(self, "TargetGroupArn", value=self.cluster_construct.target_group_arn,
                  description=f"{role} NLB target group ARN (register broker IPs)")
        CfnOutput(self, "BrokerSecurityGroupId",
                  value=self.cluster_construct.broker_sg.security_group_id,
                  description=f"{role} broker SG"
                              + (" (the demo failure lever)" if role == "primary" else ""))
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id,
                  description=f"{role} MSK VPC ID")
        CfnOutput(self, "VpcCidr", value=self.vpc.vpc_cidr_block,
                  description=f"{role} MSK VPC CIDR")

        # ── cdk-nag: deliberate demo trade-offs ───────────────────────────
        NagSuppressions.add_resource_suppressions(
            self.cluster_construct.nlb,
            [{"id": "AwsSolutions-ELB2",
              "reason": "Internal bootstrap-only NLB; access logging omitted for the demo."}],
        )
        NagSuppressions.add_resource_suppressions(
            self.cluster_construct.broker_sg,
            [{"id": "CdkNagValidationFailure",
              "reason": "EC23 cannot resolve the VPC-CIDR / client-CIDR intrinsics for the "
                        "9098 ingress rules at synth time; the rule scope is the VPC CIDR."}],
        )
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {"id": "AwsSolutions-L1",
                 "reason": "CDK AwsCustomResource provider Lambda — runtime managed by CDK."},
                {"id": "AwsSolutions-IAM4",
                 "appliesTo": ["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
                 "reason": "CDK CustomResource provider requires CloudWatch Logs access."},
                {"id": "AwsSolutions-IAM5",
                 "reason": "CDK CustomResource provider uses wildcards for cross-region exports."},
            ],
        )
