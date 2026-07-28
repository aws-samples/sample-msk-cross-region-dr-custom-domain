# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Global DNS + automatic failover (deployed in the PRIMARY region, us-east-1).

Creates:
  - a Route 53 PRIVATE hosted zone for <domain>, associated with ALL THREE VPCs
    across both regions (client VPC + primary MSK VPC + DR MSK VPC). A single
    private zone can be associated with VPCs in multiple regions within the same
    account, which is what lets the client resolve bootstrap.<domain> and the
    broker hostnames regardless of which region is active.
  - a FAILOVER record pair on bootstrap.<domain> aliasing to the primary NLB
    (PRIMARY) and the DR NLB (SECONDARY).
  - a CloudWatch alarm on the PRIMARY NLB's HealthyHostCount (this stack is in
    the primary region, so the alarm and the NLB metric are co-located).
  - a Route 53 health check backed by that alarm; when it goes unhealthy,
    bootstrap.<domain> resolves to the DR NLB.

Failover is a cross-Region DNS flip. The data path is bootstrap-only: clients
bootstrap through the NLB, then connect to the brokers directly over Transit
Gateway.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    Tags,
    aws_route53 as route53,
    aws_cloudwatch as cloudwatch,
)
from cdk_nag import NagSuppressions
from constructs import Construct


class DnsFailoverStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str,
        cluster_name: str,
        # VPCs to associate with the private zone: list of (vpc_id, region).
        associated_vpcs: list,
        # Primary NLB (same region as this stack).
        primary_nlb_dns: str,
        primary_nlb_canonical_zone_id: str,
        primary_nlb_full_name: str,
        primary_target_group_full_name: str,
        # DR NLB (other region; passed as cross-region strings).
        dr_nlb_dns: str,
        dr_nlb_canonical_zone_id: str,
        # OPTIONAL: ARC primary routing-control ARN. When provided, the primary
        # failover record is driven by a RECOVERY_CONTROL health check (operator
        # flips the control) instead of the CloudWatch-metric health check.
        primary_routing_control_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Private hosted zone associated with all three VPCs ────────────
        # L1 CfnHostedZone so we can list multiple VPCs (with their regions) and
        # get a true cross-region private zone. The first VPC seeds the zone;
        # CloudFormation associates the rest.
        first_vpc_id, first_vpc_region = associated_vpcs[0]
        hosted_zone = route53.CfnHostedZone(
            self,
            "MskPrivateHostedZone",
            name=domain_name,
            vpcs=[
                route53.CfnHostedZone.VPCProperty(vpc_id=vid, vpc_region=vregion)
                for (vid, vregion) in associated_vpcs
            ],
            hosted_zone_config=route53.CfnHostedZone.HostedZoneConfigProperty(
                comment="Cross-region MSK DR bootstrap zone",
            ),
        )

        # ── CloudWatch alarm: primary NLB has < 1 healthy target ──────────
        primary_health_alarm = cloudwatch.Alarm(
            self,
            "PrimaryHealthAlarm",
            alarm_name=f"{cluster_name}-primary-nlb-unhealthy",
            metric=cloudwatch.Metric(
                namespace="AWS/NetworkELB",
                metric_name="HealthyHostCount",
                dimensions_map={
                    "LoadBalancer": primary_nlb_full_name,
                    "TargetGroup": primary_target_group_full_name,
                },
                statistic="Minimum",
                period=Duration.minutes(1),
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=1,
            # Missing metrics (e.g. NLB gone) => treat as a failure so failover
            # still triggers rather than getting stuck.
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )

        # ── Route 53 health check driving the failover ────────────────────
        # Two modes:
        #   ARC mode (primary_routing_control_arn set): a RECOVERY_CONTROL health
        #     check whose state is the `primary` routing control's On/Off. The
        #     operator flips it for a deterministic failover. FailureThreshold /
        #     AlarmIdentifier are NOT valid for this type and must be omitted.
        #   Alarm mode (default): the original CLOUDWATCH_METRIC health check on
        #     the primary NLB's HealthyHostCount alarm (automatic failover).
        if primary_routing_control_arn:
            failover_health_check = route53.CfnHealthCheck(
                self,
                "PrimaryFailoverHealthCheck",
                health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
                    type="RECOVERY_CONTROL",
                    routing_control_arn=primary_routing_control_arn,
                ),
            )
        else:
            failover_health_check = route53.CfnHealthCheck(
                self,
                "PrimaryFailoverHealthCheck",
                health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
                    type="CLOUDWATCH_METRIC",
                    alarm_identifier=route53.CfnHealthCheck.AlarmIdentifierProperty(
                        name=primary_health_alarm.alarm_name,
                        region=self.region,
                    ),
                    insufficient_data_health_status="Unhealthy",
                ),
            )
            failover_health_check.add_dependency(primary_health_alarm.node.default_child)

        # ── Failover record pair on bootstrap.<domain> ────────────────────
        primary_record = route53.CfnRecordSet(
            self,
            "BootstrapPrimaryRecord",
            hosted_zone_id=hosted_zone.attr_id,
            name=f"bootstrap.{domain_name}.",
            type="A",
            set_identifier="primary",
            failover="PRIMARY",
            health_check_id=failover_health_check.attr_health_check_id,
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                dns_name=primary_nlb_dns,
                hosted_zone_id=primary_nlb_canonical_zone_id,
                evaluate_target_health=True,
            ),
        )
        primary_record.add_dependency(hosted_zone)

        dr_record = route53.CfnRecordSet(
            self,
            "BootstrapSecondaryRecord",
            hosted_zone_id=hosted_zone.attr_id,
            name=f"bootstrap.{domain_name}.",
            type="A",
            set_identifier="secondary",
            failover="SECONDARY",
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                dns_name=dr_nlb_dns,
                hosted_zone_id=dr_nlb_canonical_zone_id,
                evaluate_target_health=True,
            ),
        )
        dr_record.add_dependency(hosted_zone)

        Tags.of(self).add("Project", "msk-dr-cross-region-custom-domain")
        Tags.of(self).add("Purpose", "DR-failover-demo")

        # ── Outputs ───────────────────────────────────────────────────────
        CfnOutput(self, "CustomBootstrapEndpoint",
                  value=f"bootstrap.{domain_name}:9098",
                  description="Custom bootstrap endpoint (failover-routed across regions)")
        CfnOutput(self, "HostedZoneId", value=hosted_zone.attr_id,
                  description="Route 53 private hosted zone ID")
        CfnOutput(self, "PrimaryHealthAlarmName",
                  value=primary_health_alarm.alarm_name,
                  description="CloudWatch alarm driving Route 53 failover")
        CfnOutput(self, "FailoverHealthCheckId",
                  value=failover_health_check.attr_health_check_id,
                  description="Route 53 health check ID")
        CfnOutput(self, "FailoverMode",
                  value="ARC_ROUTING_CONTROL" if primary_routing_control_arn else "CLOUDWATCH_METRIC",
                  description="What drives the failover health check")

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
