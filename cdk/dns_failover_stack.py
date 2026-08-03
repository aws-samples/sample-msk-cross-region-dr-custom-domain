# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Global DNS + operator-driven failover (deployed in the PRIMARY region, us-east-1).

Creates:
  - a Route 53 PRIVATE hosted zone for <domain>, associated with ALL THREE VPCs
    across both regions (client VPC + primary MSK VPC + DR MSK VPC). A single
    private zone can be associated with VPCs in multiple regions within the same
    account, which is what lets the client resolve bootstrap.<domain> and the
    broker hostnames regardless of which region is active.
  - a FAILOVER record pair on bootstrap.<domain> aliasing to the primary NLB
    (PRIMARY) and the DR NLB (SECONDARY).
  - a Route 53 RECOVERY_CONTROL health check whose state is the ARC `primary`
    routing control's On/Off. Turning the control Off makes the health check
    unhealthy, and bootstrap.<domain> resolves to the DR NLB.

Failover is a cross-Region DNS flip driven by an operator decision, not by a
metric alarm. The data path is bootstrap-only: clients bootstrap through the NLB,
then connect to the brokers directly over Transit Gateway.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    Tags,
    aws_route53 as route53,
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
        # VPCs to associate with the private zone: list of (vpc_id, region).
        associated_vpcs: list,
        # Primary NLB (same region as this stack).
        primary_nlb_dns: str,
        primary_nlb_canonical_zone_id: str,
        # DR NLB (other region; passed as cross-region strings).
        dr_nlb_dns: str,
        dr_nlb_canonical_zone_id: str,
        # ARC primary routing-control ARN. Drives the RECOVERY_CONTROL health
        # check that backs the PRIMARY failover record.
        primary_routing_control_arn: str,
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

        # ── Route 53 health check driving the failover ────────────────────
        # A RECOVERY_CONTROL health check whose state IS the ARC `primary`
        # routing control's On/Off. The operator flips the control for a
        # deterministic cutover, with no wait on a metric evaluation window.
        # FailureThreshold / AlarmIdentifier are NOT valid for this health-check
        # type and must be omitted.
        failover_health_check = route53.CfnHealthCheck(
            self,
            "PrimaryFailoverHealthCheck",
            health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
                type="RECOVERY_CONTROL",
                routing_control_arn=primary_routing_control_arn,
            ),
        )

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
        CfnOutput(self, "FailoverHealthCheckId",
                  value=failover_health_check.attr_health_check_id,
                  description="Route 53 RECOVERY_CONTROL health check ID")

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
