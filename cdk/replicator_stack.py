# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Cross-region MSK Replicator (deployed in the TARGET / DR region, us-west-2).

For cross-region replication the Replicator MUST be created in the target
cluster's region, the source cluster must have multi-VPC private connectivity +
a resource policy (set in MskClusterStack for the primary), and the source
cluster does NOT need security groups supplied here (access is governed by the
source's resource policy). Only the target cluster's VPC config is provided.

Topics are replicated with IDENTICAL names so a client failing over via the same
bootstrap domain finds the same topic names on the DR cluster.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    Fn,
    Tags,
    aws_msk as msk,
    aws_iam as iam,
)
from constructs import Construct
from cdk_nag import NagSuppressions


class ReplicatorStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_name: str,
        source_cluster_arn: str,   # primary (us-east-1)
        target_cluster_arn: str,   # dr (us-west-2, this region)
        source_subnet_ids: list,   # primary cluster's own subnets (us-east-1)
        target_subnet_ids: list,
        target_broker_sg_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Replicator service role ───────────────────────────────────────
        replicator_role = iam.Role(
            self,
            "ReplicatorServiceRole",
            assumed_by=iam.ServicePrincipal("kafka.amazonaws.com"),
            description="Service execution role for the cross-region MSK Replicator",
        )
        # Data-plane permissions on both clusters' topics/groups. Each cluster
        # ARN carries its own region; split on ":cluster/" to rebuild scoped
        # topic/group ARNs without assuming a single region.
        for c_arn in (source_cluster_arn, target_cluster_arn):
            arn_head = Fn.select(0, Fn.split(":cluster/", c_arn))
            suffix = Fn.select(1, Fn.split(":cluster/", c_arn))
            replicator_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "kafka-cluster:Connect",
                        "kafka-cluster:DescribeCluster",
                        "kafka-cluster:AlterCluster",
                        "kafka-cluster:DescribeTopic",
                        "kafka-cluster:CreateTopic",
                        "kafka-cluster:AlterTopic",
                        "kafka-cluster:WriteData",
                        "kafka-cluster:ReadData",
                        "kafka-cluster:DescribeTopicDynamicConfiguration",
                        "kafka-cluster:AlterTopicDynamicConfiguration",
                        "kafka-cluster:DescribeGroup",
                        "kafka-cluster:AlterGroup",
                    ],
                    resources=[
                        c_arn,
                        f"{arn_head}:topic/{suffix}/*",
                        f"{arn_head}:group/{suffix}/*",
                    ],
                )
            )
        # Cross-region source access also needs the multi-VPC describe/connect.
        replicator_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "kafka:DescribeClusterV2",
                    "kafka:GetBootstrapBrokers",
                    "kafka:CreateVpcConnection",
                    "kafka:DescribeVpcConnection",
                ],
                resources=["*"],
            )
        )

        # ── Replicator (created in the target region) ─────────────────────
        # NOTE: AWS::MSK::Replicator does not support in-place updates to
        # TopicReplication settings; a change requires REPLACING the replicator.
        # MSK allows only one replicator per source/target pair, so if you change
        # settings, bump the construct id and delete the old one first.
        replicator = msk.CfnReplicator(
            self,
            "MskReplicator",
            replicator_name=f"{cluster_name}-xregion-replicator",
            service_execution_role_arn=replicator_role.role_arn,
            kafka_clusters=[
                # SOURCE (primary, us-east-1): cross-region. SubnetIds is required
                # (min 2) and must be the SOURCE cluster's own subnets. No security
                # group is supplied for a cross-region source — access is governed
                # by the source cluster's resource policy + multi-VPC connectivity
                # (set in MskClusterStack), per the MSK cross-region docs.
                msk.CfnReplicator.KafkaClusterProperty(
                    amazon_msk_cluster=msk.CfnReplicator.AmazonMskClusterProperty(
                        msk_cluster_arn=source_cluster_arn,
                    ),
                    vpc_config=msk.CfnReplicator.KafkaClusterClientVpcConfigProperty(
                        subnet_ids=source_subnet_ids,
                    ),
                ),
                # TARGET (dr, us-west-2): this region's subnets + broker SG.
                msk.CfnReplicator.KafkaClusterProperty(
                    amazon_msk_cluster=msk.CfnReplicator.AmazonMskClusterProperty(
                        msk_cluster_arn=target_cluster_arn,
                    ),
                    vpc_config=msk.CfnReplicator.KafkaClusterClientVpcConfigProperty(
                        subnet_ids=target_subnet_ids,
                        security_group_ids=[target_broker_sg_id],
                    ),
                ),
            ],
            replication_info_list=[
                msk.CfnReplicator.ReplicationInfoProperty(
                    source_kafka_cluster_arn=source_cluster_arn,
                    target_kafka_cluster_arn=target_cluster_arn,
                    target_compression_type="NONE",
                    topic_replication=msk.CfnReplicator.TopicReplicationProperty(
                        topics_to_replicate=["demo.*", "orders.*"],
                        copy_topic_configurations=True,
                        detect_and_copy_new_topics=True,
                        # IDENTICAL keeps "demo.heartbeat" -> "demo.heartbeat" so a
                        # client failing over via the same bootstrap domain finds
                        # the same topic names on the DR cluster.
                        topic_name_configuration=msk.CfnReplicator.ReplicationTopicNameConfigurationProperty(
                            type="IDENTICAL",
                        ),
                    ),
                    consumer_group_replication=msk.CfnReplicator.ConsumerGroupReplicationProperty(
                        consumer_groups_to_replicate=[".*"],
                        synchronise_consumer_group_offsets=True,
                        detect_and_copy_new_consumer_groups=True,
                    ),
                )
            ],
        )
        replicator.node.add_dependency(replicator_role)

        Tags.of(self).add("Project", "msk-dr-cross-region-custom-domain")
        Tags.of(self).add("Purpose", "DR-failover-demo")

        CfnOutput(self, "ReplicatorArn", value=replicator.attr_replicator_arn,
                  description="Cross-region MSK Replicator ARN (primary -> DR)")

        NagSuppressions.add_resource_suppressions(
            replicator_role,
            [{"id": "AwsSolutions-IAM5",
              "reason": "Replicator service role needs topic/group wildcards within each "
                        "cluster ARN to replicate all matching topics/groups; cross-region "
                        "multi-VPC connect/describe actions have no resource-level scoping."}],
            apply_to_children=True,
        )
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {"id": "AwsSolutions-L1",
                 "reason": "CDK AwsCustomResource provider Lambda — runtime managed by CDK."},
                {"id": "AwsSolutions-IAM4",
                 "appliesTo": ["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
                 "reason": "CDK CustomResource provider requires CloudWatch Logs access."},
            ],
        )
