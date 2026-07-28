# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Transit Gateway networking for cross-region DR.

Topology (single account, us-east-1 primary + us-west-2 DR):

    Client VPC (us-east-1, 10.2.0.0/16) ─┐
                                          ├─ TGW-east ═══ peering ═══ TGW-west ── DR MSK VPC (us-west-2, 10.1.0.0/16)
    Primary MSK VPC (us-east-1, 10.0/16)─┘

The client must reach BOTH MSK VPCs' private broker IPs directly (bootstrap
through the NLB, then direct broker connection). That means:
  - each VPC is attached to the TGW in its own region;
  - the two TGWs are peered cross-region;
  - TGW route tables + each VPC's subnet route tables carry routes to the remote
    MSK CIDRs.

Two stacks are instantiated from this module:
  - NetworkStack(role="primary") in us-east-1: TGW-east, attaches client + primary
    MSK VPCs, INITIATES the cross-region peering to TGW-west.
  - NetworkStack(role="dr") in us-west-2: TGW-west, attaches DR MSK VPC, ACCEPTS
    the peering (via a custom resource — plain CloudFormation cannot accept a
    cross-region peering request).

Cross-region static routes that point at the peering attachment are added only
after the peering is "available", so they live on whichever side can depend on
acceptance. See app.py for how the pieces are wired with crossRegionReferences.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    Tags,
    aws_ec2 as ec2,
    custom_resources as cr,
    aws_iam as iam,
)
from constructs import Construct
from cdk_nag import NagSuppressions


class TransitGatewayStack(Stack):
    """A regional Transit Gateway with VPC attachments + route wiring."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        role: str,  # "primary" | "dr"
        # VPCs to attach in THIS region. Each entry is
        #   (name, vpc, private_subnet_ids, remote_cidrs_for_this_vpc)
        # where remote_cidrs_for_this_vpc lists EVERY other VPC CIDR this VPC
        # reaches via the TGW — both same-region and cross-region peers. VPCs
        # joined only by a TGW are not directly peered, so each destination CIDR
        # needs an explicit route, including same-region ones.
        attachments: list,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.role = role

        # ── Transit Gateway ───────────────────────────────────────────────
        self.tgw = ec2.CfnTransitGateway(
            self,
            "Tgw",
            description=f"DR demo TGW ({role})",
            # Auto-accept shared attachments within the same account; explicit
            # default route table assoc/propagation keeps the demo simple.
            auto_accept_shared_attachments="enable",
            default_route_table_association="enable",
            default_route_table_propagation="enable",
            tags=[{"key": "Name", "value": f"msk-dr-tgw-{role}"}],
        )

        # ── VPC attachments (one per VPC in this region) ──────────────────
        self.vpc_attachments = {}
        for name, vpc, subnet_ids, vpc_remote_cidrs in attachments:
            att = ec2.CfnTransitGatewayAttachment(
                self,
                f"{name}Attachment",
                transit_gateway_id=self.tgw.ref,
                vpc_id=vpc.vpc_id,
                subnet_ids=subnet_ids,
                tags=[{"key": "Name", "value": f"msk-dr-{name}"}],
            )
            att.add_dependency(self.tgw)
            self.vpc_attachments[name] = att

            # Each VPC's private subnets need a route to EVERY other VPC CIDR it
            # must reach via TGW (same-region peers + cross-region peers).
            # Logical IDs are derived from the CIDR (not a positional index) so
            # that adding/removing a remote CIDR doesn't renumber the others and
            # cause an "AlreadyExists" route collision on update.
            for i, subnet in enumerate(vpc.private_subnets):
                for cidr in vpc_remote_cidrs:
                    cidr_id = cidr.replace(".", "_").replace("/", "_")
                    route = ec2.CfnRoute(
                        self,
                        f"{name}Subnet{i}To{cidr_id}Route",
                        route_table_id=subnet.route_table.route_table_id,
                        destination_cidr_block=cidr,
                        transit_gateway_id=self.tgw.ref,
                    )
                    route.add_dependency(att)

        Tags.of(self).add("Project", "msk-dr-cross-region-custom-domain")
        Tags.of(self).add("Purpose", "DR-failover-demo")

        CfnOutput(self, "TgwId", value=self.tgw.ref,
                  description=f"{role} Transit Gateway ID")


class TgwPeeringStack(Stack):
    """Initiates (primary side) the cross-region TGW peering attachment.

    Deployed in the PRIMARY region. References the DR TGW id via a cross-region
    export. Acceptance happens on the DR side (TgwPeeringAccepterStack).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        local_tgw_id: str,
        peer_tgw_id: str,
        peer_region: str,
        peer_account: str,
        # (tgw_route_table_id-less) remote CIDR -> add a TGW route to peering.
        remote_cidrs: list,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.peering = ec2.CfnTransitGatewayPeeringAttachment(
            self,
            "TgwPeering",
            transit_gateway_id=local_tgw_id,
            peer_transit_gateway_id=peer_tgw_id,
            peer_region=peer_region,
            peer_account_id=peer_account,
            tags=[{"key": "Name", "value": "msk-dr-tgw-peering"}],
        )

        CfnOutput(self, "PeeringAttachmentId",
                  value=self.peering.attr_transit_gateway_attachment_id,
                  description="Cross-region TGW peering attachment ID")


class TgwPeeringAccepterStack(Stack):
    """Accepts the cross-region TGW peering on the DR side.

    Plain CloudFormation cannot accept a peering attachment that was requested in
    another region, so we use an AwsCustomResource that calls
    AcceptTransitGatewayPeeringAttachment. After acceptance, TGW routes to the
    remote CIDRs are added to each TGW's default route table by the post-deploy
    networking script (scripts/wire_tgw_routes.sh), because the route's target
    (the peering attachment) must be 'available' first.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        peering_attachment_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        accept = cr.AwsCustomResource(
            self,
            "AcceptTgwPeering",
            on_create=cr.AwsSdkCall(
                service="EC2",
                action="acceptTransitGatewayPeeringAttachment",
                parameters={"TransitGatewayAttachmentId": peering_attachment_id},
                physical_resource_id=cr.PhysicalResourceId.of(peering_attachment_id),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["ec2:AcceptTransitGatewayPeeringAttachment"],
                    resources=[
                        f"arn:aws:ec2:{Stack.of(self).region}:{Stack.of(self).account}:transit-gateway-attachment/{peering_attachment_id}",
                    ],
                )
            ]),
        )
        self.accept = accept

        CfnOutput(self, "PeeringAccepted", value=peering_attachment_id,
                  description="Accepted cross-region TGW peering attachment ID")

        # The AwsCustomResource provisions a CDK-managed Lambda + role. These
        # findings are on that generated provider, not on demo code.
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "CDK's AwsCustomResource provider Lambda uses the AWS-managed "
                              "AWSLambdaBasicExecutionRole for CloudWatch Logs only.",
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "CDK AwsCustomResource provider uses wildcards for the Lambda "
                              "execution role's log group ARN suffix.",
                },
            ],
        )
