# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Client stack (deployed in the PRIMARY region, us-east-1).

A dedicated client VPC — SEPARATE from both MSK VPCs — holding an SSM-managed
EC2 Kafka client (load generator + demo control plane). The client reaches both
MSK clusters' brokers directly over Transit Gateway after bootstrapping through
the custom domain.

This stack only creates the VPC + client. The TGW attachment + routes are added
by the networking stack; the broker endpoints / health-check / SG-lever values
the demo scripts need are injected as env vars from values passed in by app.py.
"""

import os

from aws_cdk import (
    Stack,
    CfnOutput,
    Fn,
    Tags,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3_assets as s3_assets,
)
from constructs import Construct
from cdk_nag import NagSuppressions


class ClientStack(Stack):
    """Separate client VPC + SSM EC2 Kafka client."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc_cidr: str,
        domain_name: str,
        primary_region: str,
        dr_region: str,
        primary_cluster_arn: str,
        dr_cluster_arn: str,
        primary_broker_sg_id: str,
        primary_msk_cidr: str,
        # OPTIONAL ARC wiring (present when use_arc): the primary routing control
        # the demo scripts flip, and its cluster (whose endpoints serve the flip).
        primary_routing_control_arn: str | None = None,
        arc_cluster_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Client VPC (separate from MSK; non-overlapping CIDR) ──────────
        self.vpc = ec2.Vpc(
            self,
            "ClientVpc",
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

        self.client_sg = ec2.SecurityGroup(
            self,
            "ClientSecurityGroup",
            vpc=self.vpc,
            description="Security group for the Kafka client EC2 instance",
            allow_all_outbound=True,
        )

        # ── Client IAM role ───────────────────────────────────────────────
        client_role = iam.Role(
            self,
            "KafkaClientRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )

        # IAM-auth data-plane permissions on BOTH clusters (cross-region ARNs).
        # The cluster ARN embeds the region, so the topic/group resource ARNs are
        # region-correct for each cluster.
        for cluster_arn in (primary_cluster_arn, dr_cluster_arn):
            # arn:aws:kafka:<region>:<acct>:cluster/<name>/<uuid>
            # topic/group ARNs share the cluster path: .../<name>/<uuid>/...
            client_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "kafka-cluster:Connect",
                        "kafka-cluster:DescribeCluster",
                        "kafka-cluster:AlterCluster",
                    ],
                    resources=[cluster_arn],
                )
            )
            # topic/* and group/* under the same cluster path. cluster_arn is a
            # CFN token (string .replace won't work), and it may be in a DIFFERENT
            # region than this stack — so we split on ":cluster/" to keep the
            # source ARN's own region/account prefix, then rebuild the resource.
            #   arn:aws:kafka:<region>:<acct>:cluster/<name>/<uuid>
            #   -> [ "arn:aws:kafka:<region>:<acct>", "<name>/<uuid>" ]
            arn_head = Fn.select(0, Fn.split(":cluster/", cluster_arn))
            cluster_suffix = Fn.select(1, Fn.split(":cluster/", cluster_arn))
            topic_arn = f"{arn_head}:topic/{cluster_suffix}/*"
            group_arn = f"{arn_head}:group/{cluster_suffix}/*"
            client_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "kafka-cluster:DescribeTopic",
                        "kafka-cluster:CreateTopic",
                        "kafka-cluster:DeleteTopic",
                        "kafka-cluster:WriteData",
                        "kafka-cluster:ReadData",
                    ],
                    resources=[topic_arn],
                )
            )
            client_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "kafka-cluster:AlterGroup",
                        "kafka-cluster:DescribeGroup",
                    ],
                    resources=[group_arn],
                )
            )

        # Control-plane describe/list for broker discovery (both regions).
        client_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "kafka:DescribeCluster",
                    "kafka:DescribeClusterV2",
                    "kafka:GetBootstrapBrokers",
                    "kafka:ListClusters",
                ],
                resources=["*"],
            )
        )
        # Register/inspect NLB targets (in both regions) from the client.
        # Scoped to account — target groups in both regions.
        client_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticloadbalancing:DescribeTargetGroups",
                    "elasticloadbalancing:DescribeTargetHealth",
                ],
                resources=["*"],
            )
        )
        client_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticloadbalancing:RegisterTargets",
                    "elasticloadbalancing:DeregisterTargets",
                ],
                resources=[
                    f"arn:aws:elasticloadbalancing:*:{Stack.of(self).account}:targetgroup/*",
                ],
            )
        )
        # Failover demo levers + observability (read-only actions on *):
        client_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeNetworkAcls",
                    "cloudwatch:DescribeAlarms",
                    "route53:GetHealthCheckStatus",
                    "route53:ListResourceRecordSets",
                    "cloudformation:DescribeStacks",
                ],
                resources=["*"],
            )
        )
        # Failover write actions scoped to the primary broker SG and VPC NACLs.
        client_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:AuthorizeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupIngress",
                ],
                resources=[
                    f"arn:aws:ec2:{primary_region}:{Stack.of(self).account}:security-group/{primary_broker_sg_id}",
                ],
            )
        )
        client_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:CreateNetworkAclEntry",
                    "ec2:DeleteNetworkAclEntry",
                ],
                resources=[
                    f"arn:aws:ec2:{primary_region}:{Stack.of(self).account}:network-acl/*",
                ],
            )
        )
        client_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["cloudwatch:SetAlarmState"],
                resources=[
                    f"arn:aws:cloudwatch:{primary_region}:{Stack.of(self).account}:alarm:*",
                ],
            )
        )
        # ARC routing-control failover (lever 1): describe the cluster to find its
        # endpoints, then flip the primary routing control On/Off via the cluster
        # (recovery-cluster) data plane. Only granted when ARC is wired.
        if primary_routing_control_arn:
            client_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "route53-recovery-control-config:DescribeCluster",
                        "route53-recovery-control-config:DescribeRoutingControl",
                    ],
                    resources=[arc_cluster_arn, primary_routing_control_arn],
                )
            )
            client_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "route53-recovery-cluster:GetRoutingControlState",
                        "route53-recovery-cluster:UpdateRoutingControlState",
                        "route53-recovery-cluster:UpdateRoutingControlStates",
                    ],
                    resources=[primary_routing_control_arn],
                )
            )

        # ── Demo scripts as an S3 asset, pulled at boot ──────────────────
        scripts_asset = s3_assets.Asset(
            self,
            "DemoScriptsAsset",
            path=os.path.join(os.path.dirname(__file__), "..", "scripts"),
        )
        scripts_asset.grant_read(client_role)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -ex",
            "",
            "# Install Java + tooling",
            "dnf install -y java-17-amazon-corretto-headless wget unzip bind-utils jq",
            "",
            "# Apache Kafka CLI. Download is resilient: try multiple Apache",
            "# mirrors with retries. A single flaky mirror under `set -ex` would",
            "# otherwise abort the whole bootstrap and leave the host unusable.",
            "KAFKA_VERSION=3.6.0",
            "SCALA_VERSION=2.13",
            'KAFKA_TGZ="kafka_${SCALA_VERSION}-${KAFKA_VERSION}.tgz"',
            "kafka_downloaded=0",
            'for base in \\',
            '    "https://archive.apache.org/dist/kafka/${KAFKA_VERSION}" \\',
            '    "https://downloads.apache.org/kafka/${KAFKA_VERSION}" \\',
            '    "https://dlcdn.apache.org/kafka/${KAFKA_VERSION}"; do',
            '    if wget -q --tries=3 --timeout=30 "${base}/${KAFKA_TGZ}" -O /tmp/kafka.tgz && [ -s /tmp/kafka.tgz ]; then',
            '        kafka_downloaded=1; break',
            "    fi",
            "done",
            '[ "$kafka_downloaded" = "1" ] || { echo "Kafka download failed from all mirrors"; exit 1; }',
            "tar -xzf /tmp/kafka.tgz -C /opt/",
            "ln -sfn /opt/kafka_${SCALA_VERSION}-${KAFKA_VERSION} /opt/kafka",
            "",
            "# MSK IAM auth library (pinned), with retries.",
            "MSK_IAM_AUTH_VERSION=2.2.0",
            'wget -q --tries=3 --timeout=30 "https://github.com/aws/aws-msk-iam-auth/releases/download/v${MSK_IAM_AUTH_VERSION}/aws-msk-iam-auth-${MSK_IAM_AUTH_VERSION}-all.jar" -O "/opt/kafka/libs/aws-msk-iam-auth-${MSK_IAM_AUTH_VERSION}-all.jar"',
            "",
            "# Client config. Hostname verification is ON (algorithm=https): the",
            "# bootstrap listener presents a certificate that matches bootstrap.<domain>,",
            "# and the direct broker connections present Amazon's public certificate.",
            "# The connection is SASL_SSL-encrypted and IAM-authenticated.",
            "cat <<'EOF' > /opt/kafka/client-iam.properties",
            "security.protocol=SASL_SSL",
            "sasl.mechanism=AWS_MSK_IAM",
            "sasl.jaas.config=software.amazon.msk.auth.iam.IAMLoginModule required;",
            "sasl.client.callback.handler.class=software.amazon.msk.auth.iam.IAMClientCallbackHandler",
            "ssl.endpoint.identification.algorithm=https",
            "ssl.truststore.location=/opt/kafka/kafka.truststore.jks",
            "ssl.truststore.password=changeit",
            "EOF",
            "chmod 644 /opt/kafka/client-iam.properties",
            "",
            "# Environment for the demo scripts. Cross-region: the PRIMARY cluster",
            "# and its failover lever live in the primary region; the DR cluster is",
            "# in the dr region. Scripts default to the primary region but accept",
            "# --region overrides.",
            'echo "export PATH=\\$PATH:/opt/kafka/bin" > /etc/profile.d/kafka.sh',
            'echo "export KAFKA_CLIENT_CONFIG=/opt/kafka/client-iam.properties" >> /etc/profile.d/kafka.sh',
            f'echo "export BOOTSTRAP_DOMAIN=bootstrap.{domain_name}:9098" >> /etc/profile.d/kafka.sh',
            f'echo "export PRIMARY_REGION={primary_region}" >> /etc/profile.d/kafka.sh',
            f'echo "export DR_REGION={dr_region}" >> /etc/profile.d/kafka.sh',
            f'echo "export PRIMARY_CLUSTER_ARN={primary_cluster_arn}" >> /etc/profile.d/kafka.sh',
            f'echo "export DR_CLUSTER_ARN={dr_cluster_arn}" >> /etc/profile.d/kafka.sh',
            f'echo "export PRIMARY_BROKER_SG={primary_broker_sg_id}" >> /etc/profile.d/kafka.sh',
            # CIDRs failback.sh re-adds to the broker SG (both 9098 ingress rules).
            f'echo "export PRIMARY_MSK_CIDR={primary_msk_cidr}" >> /etc/profile.d/kafka.sh',
            f'echo "export CLIENT_CIDR={vpc_cidr}" >> /etc/profile.d/kafka.sh',
            # The CloudWatch alarm + Route53 health-check live in the DNS stack
            # (primary region). watch_failover.sh discovers them by stack name at
            # runtime to avoid a circular stack dependency (the DNS stack already
            # needs this client VPC's id for the private-zone association).
            'echo "export DNS_STACK_NAME=MskDnsFailover" >> /etc/profile.d/kafka.sh',
            # ARC routing-control ARNs (present only when use_arc). The failover
            # scripts flip PRIMARY_ROUTING_CONTROL_ARN via ARC_CLUSTER_ARN's
            # endpoints for a deterministic, operator-driven cutover.
            *([
                f'echo "export PRIMARY_ROUTING_CONTROL_ARN={primary_routing_control_arn}" >> /etc/profile.d/kafka.sh',
                f'echo "export ARC_CLUSTER_ARN={arc_cluster_arn}" >> /etc/profile.d/kafka.sh',
            ] if primary_routing_control_arn else []),
            "",
            "# Fetch the demo scripts bundled as an S3 asset",
            f"aws s3 cp s3://{scripts_asset.s3_bucket_name}/{scripts_asset.s3_object_key} /tmp/scripts.zip --region {primary_region}",
            "unzip -o /tmp/scripts.zip -d /opt/kafka/",
            "chmod +x /opt/kafka/*.sh || true",
            "",
            "# Build the client truststore now that bootstrap-ca.pem (bundled in the",
            "# scripts asset) is on disk. Start from the JVM default cacerts, which",
            "# already trusts Amazon's public CA for the DIRECT broker connections",
            "# after bootstrap, then add the bootstrap CA for the NLB's TLS listener.",
            "# This keeps ssl.endpoint.identification.algorithm=https working on both",
            "# legs. See scripts/gen_bootstrap_cert.sh.",
            "JAVA_HOME=$(dirname $(dirname $(readlink -f $(which java))))",
            'cp "$JAVA_HOME/lib/security/cacerts" /opt/kafka/kafka.truststore.jks',
            "keytool -importcert -noprompt -alias msk-bootstrap-ca \\",
            "    -file /opt/kafka/bootstrap-ca.pem \\",
            "    -keystore /opt/kafka/kafka.truststore.jks \\",
            "    -storepass changeit",
            "chmod 644 /opt/kafka/kafka.truststore.jks",
            "",
            "echo 'Client ready. Connect via SSM, run: bash -l, then use the scripts in /opt/kafka.'",
        )

        self.client_instance = ec2.Instance(
            self,
            "KafkaClientInstance",
            vpc=self.vpc,
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM
            ),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=self.client_sg,
            role=client_role,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            user_data=user_data,
            detailed_monitoring=True,
            require_imdsv2=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        20, encrypted=True,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                    ),
                )
            ],
        )

        Tags.of(self).add("Project", "msk-dr-cross-region-custom-domain")
        Tags.of(self).add("Purpose", "DR-failover-demo")

        # ── Outputs ───────────────────────────────────────────────────────
        CfnOutput(self, "ClientVpcId", value=self.vpc.vpc_id,
                  description="Client VPC ID")
        CfnOutput(self, "ClientVpcCidr", value=self.vpc.vpc_cidr_block,
                  description="Client VPC CIDR")
        CfnOutput(self, "ClientInstanceId", value=self.client_instance.instance_id,
                  description="EC2 client instance ID (connect via SSM)")

        # ── cdk-nag suppressions ──────────────────────────────────────────
        NagSuppressions.add_resource_suppressions(
            client_role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AmazonSSMManagedInstanceCore is the AWS-recommended managed "
                              "policy for Session Manager access; it replaces SSH + a bastion.",
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonSSMManagedInstanceCore"
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Two kinds of wildcards, both intentional: (1) discovery and NLB "
                              "target registration need account-wide describe/list calls that "
                              "have no resource-level scoping (kafka:ListClusters, "
                              "elasticloadbalancing:Describe*, ec2:Describe*, cross-region "
                              "route53/cloudwatch); (2) data-plane kafka-cluster:* actions use a "
                              "topic/<cluster>/* and group/<cluster>/* wildcard so the client can "
                              "use any topic or consumer group within the two demo clusters.",
                },
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            self.client_instance,
            [{"id": "AwsSolutions-EC29",
              "reason": "Disposable demo client; termination protection would only get in the "
                        "way of cdk destroy cleanup."}],
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
                {"id": "AwsSolutions-IAM5",
                 "reason": "CDK CustomResource provider uses wildcards for cross-region exports."},
            ],
        )
