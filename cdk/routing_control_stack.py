# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Route 53 Application Recovery Controller (ARC) — classic routing controls.

Gives the DR failover an OPERATOR-CONTROLLED, deterministic switch instead of
(only) waiting on a CloudWatch metric alarm. The pieces:

  - an ARC CLUSTER: the highly-available (5-endpoint, multi-region) data plane
    that serves routing-control state even during a regional impairment. This is
    the component that carries an hourly cost, so destroy the demo when done.
  - a CONTROL PANEL holding the routing control.
  - a single ROUTING CONTROL: `primary`, an On/Off switch. On = serve the
    PRIMARY region; Off = fail over to DR.

Why a SINGLE control (and no safety rule): the DNS topology is a Route 53
FAILOVER PAIR — a PRIMARY record (health-checked by this control) and a
SECONDARY record that is the native, always-available fallback. When the primary
record's health check goes unhealthy, Route 53 serves SECONDARY regardless of
whether SECONDARY has its own check, so a single control CANNOT blackhole
traffic. An assertion safety rule ("at least one control On") would therefore
guard against a condition that cannot occur here, while adding a confusing extra
step to failover (new controls default to Off, so the rule would force flipping a
second, decorative control On first). The clean active/passive pattern is one
control on the PRIMARY record — matching the aws-samples `arc-iad` reference.

The Route 53 RECOVERY_CONTROL health check that consumes the `primary` routing
control is created in the DNS stack (Route 53 is global / us-east-1), which is
why this stack only exports the routing-control ARN.

ARC's recovery-control CONFIG plane lives in us-west-2, so this stack is deployed
there. Flipping state at run time uses the CLUSTER endpoints (any region), which
is what the demo scripts do.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    Tags,
    aws_route53recoverycontrol as arc,
)
from cdk_nag import NagSuppressions
from constructs import Construct


class RoutingControlStack(Stack):
    """ARC cluster + control panel + primary/dr routing controls + safety rule."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── ARC cluster (the HA data plane; hourly-billed) ────────────────
        self.cluster = arc.CfnCluster(
            self,
            "ArcCluster",
            name=f"{cluster_name}-arc",
        )

        # ── Control panel ─────────────────────────────────────────────────
        self.control_panel = arc.CfnControlPanel(
            self,
            "ControlPanel",
            name=f"{cluster_name}-panel",
            cluster_arn=self.cluster.attr_cluster_arn,
        )

        # ── Routing control: primary ─────────────────────────────────────
        # A single On/Off switch. On = PRIMARY healthy (serve us-east-1);
        # Off = fail over to DR. No safety rule (see module docstring): the
        # failover-record topology makes a blackhole impossible with one control.
        self.primary_rc = arc.CfnRoutingControl(
            self,
            "PrimaryRoutingControl",
            name="primary",
            cluster_arn=self.cluster.attr_cluster_arn,
            control_panel_arn=self.control_panel.attr_control_panel_arn,
        )

        Tags.of(self).add("Project", "msk-dr-cross-region-custom-domain")
        Tags.of(self).add("Purpose", "DR-failover-demo")

        # ── Outputs ───────────────────────────────────────────────────────
        CfnOutput(self, "ArcClusterArn", value=self.cluster.attr_cluster_arn,
                  description="ARC cluster ARN (flip state via its endpoints)")
        CfnOutput(self, "PrimaryRoutingControlArn",
                  value=self.primary_rc.attr_routing_control_arn,
                  description="Primary routing control ARN (drives the failover health check)")

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

    # Convenience accessors -------------------------------------------------
    @property
    def cluster_arn(self) -> str:
        return self.cluster.attr_cluster_arn

    @property
    def primary_routing_control_arn(self) -> str:
        return self.primary_rc.attr_routing_control_arn
