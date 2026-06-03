"""ECS self-discovery for sandbox pool configuration.

Production deploys typically run the agent inside an ECS task. That task
*already knows* which cluster, subnets, security group, and task
definition it's running with — so requiring the operator to repeat all
of that as SANDBOX_* env vars is bookkeeping the agent could do for
itself.

`discover_from_ecs_metadata(region)` queries the standard ECS task
metadata endpoint (set as `ECS_CONTAINER_METADATA_URI_V4` in every ECS
task since 2020) plus two boto3 lookups to derive:

  - cluster_name        (from the task metadata's `Cluster` ARN)
  - task_definition_arn (reconstructed from `Family` + `Revision` +
                          account id parsed out of the task ARN)
  - container_name      (first container's `Name` field)
  - subnet_ids          (single-element list, from the task's ENI)
  - security_group_id   (first SG attached to the task's ENI)

Outside ECS (no metadata URI in env), returns `None` — callers must
fall back to explicit env vars.

Errors at any stage are logged at WARNING and result in the relevant
key being *omitted* from the returned dict rather than the whole
function raising. The intent is "discover what you can; let the caller
require what's missing." That way partial discovery still helps in
unusual environments (e.g. metadata endpoint up but
`ecs:DescribeTasks` IAM not granted).

IAM needed (assumes the agent task role already has these for the pool
to function — they're not new requirements introduced by discovery):
  - ecs:DescribeTasks
  - ec2:DescribeNetworkInterfaces
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def discover_from_ecs_metadata(region: str) -> dict[str, Any] | None:
    """Return a dict of discovered sandbox config defaults, or None outside ECS.

    The returned dict may contain any subset of:
      `cluster_name`, `task_definition_arn`, `container_name`,
      `subnet_ids` (list[str]), `security_group_id`.

    Keys are present only when the corresponding lookup succeeded.
    Missing keys mean the caller must source the value from an env var.
    """
    metadata_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not metadata_uri:
        # Not running in an ECS task. Caller should treat this as
        # "no discovery available" and rely on explicit env vars.
        return None

    discovered: dict[str, Any] = {}

    metadata = _fetch_task_metadata(metadata_uri)
    if metadata is None:
        # HTTP fetch failed; nothing more we can do without it.
        # Return empty (not None) so caller knows discovery was *attempted*
        # — distinguishes "not in ECS" from "in ECS but metadata broken."
        return discovered

    cluster_arn = (metadata.get("Cluster") or "").strip()
    task_arn = (metadata.get("TaskARN") or "").strip()
    family = (metadata.get("Family") or "").strip()
    revision = metadata.get("Revision")

    if cluster_arn:
        # ARN tail. Examples:
        #   arn:aws:ecs:eu-central-1:123:cluster/MyCluster -> "MyCluster"
        #   "MyCluster" (rare, but accept it) -> "MyCluster"
        discovered["cluster_name"] = cluster_arn.rsplit("/", 1)[-1]

    if family and revision is not None and task_arn:
        # Reconstruct the task-definition ARN. The metadata endpoint
        # gives us Family + Revision but not the full ARN, so we lift
        # account id out of the task ARN to assemble it.
        # task_arn format: arn:aws:ecs:<region>:<account>:task/<cluster>/<task-id>
        parts = task_arn.split(":", 5)
        if len(parts) >= 5:
            account = parts[4]
            discovered["task_definition_arn"] = (
                f"arn:aws:ecs:{region}:{account}:task-definition/{family}:{revision}"
            )

    containers = metadata.get("Containers") or []
    for c in containers:
        name = c.get("Name")
        if name:
            # First container's name. For Flavour 1 (merged image) the
            # agent task has exactly one container, so "first" is fine.
            # If a future setup has multiple, the operator can override
            # via SANDBOX_CONTAINER_NAME.
            discovered["container_name"] = name
            break

    # Network info needs two API calls. Skip the lot if we don't have
    # the inputs for the first one.
    if cluster_arn and task_arn:
        eni_id = _lookup_eni_id(region=region, cluster=cluster_arn, task_arn=task_arn)
        if eni_id:
            net = _lookup_eni_network(region=region, eni_id=eni_id)
            if net:
                if net.get("subnet_id"):
                    # subnet_ids is a list to match PoolConfig's contract;
                    # an ECS task lives in exactly one subnet so the list
                    # is length 1.
                    discovered["subnet_ids"] = [net["subnet_id"]]
                if net.get("security_group_id"):
                    discovered["security_group_id"] = net["security_group_id"]

    return discovered


def _fetch_task_metadata(metadata_uri: str) -> dict[str, Any] | None:
    """GET <uri>/task and return the parsed JSON, or None on any failure."""
    try:
        import httpx

        resp = httpx.get(f"{metadata_uri}/task", timeout=5.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("ECS task metadata fetch failed: %s", e)
        return None


def _lookup_eni_id(*, region: str, cluster: str, task_arn: str) -> str | None:
    """Return the ENI id attached to the running task, via ecs:DescribeTasks."""
    try:
        import boto3

        ecs = boto3.client("ecs", region_name=region)
        resp = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
    except Exception as e:  # noqa: BLE001
        log.warning("ecs:DescribeTasks self-lookup failed: %s", e)
        return None

    tasks = resp.get("tasks", []) or []
    if not tasks:
        log.warning("ecs:DescribeTasks returned no task for self ARN %s", task_arn)
        return None

    for attachment in tasks[0].get("attachments", []) or []:
        if attachment.get("type") != "ElasticNetworkInterface":
            continue
        for detail in attachment.get("details", []) or []:
            if detail.get("name") == "networkInterfaceId":
                value = detail.get("value")
                if value:
                    return value
    log.warning("no ENI attachment found on self task %s", task_arn)
    return None


def _lookup_eni_network(*, region: str, eni_id: str) -> dict[str, str] | None:
    """Return {subnet_id, security_group_id} for the given ENI, or None."""
    try:
        import boto3

        ec2 = boto3.client("ec2", region_name=region)
        resp = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
    except Exception as e:  # noqa: BLE001
        log.warning("ec2:DescribeNetworkInterfaces lookup failed for %s: %s", eni_id, e)
        return None

    nis = resp.get("NetworkInterfaces", []) or []
    if not nis:
        log.warning("ec2:DescribeNetworkInterfaces returned no NIs for %s", eni_id)
        return None

    ni = nis[0]
    out: dict[str, str] = {}
    subnet_id = ni.get("SubnetId")
    if subnet_id:
        out["subnet_id"] = subnet_id

    groups = ni.get("Groups", []) or []
    if groups:
        # Pick the first SG. ECS tasks typically have one SG when launched
        # via run_task with one securityGroups entry. If a task is somehow
        # attached to multiple, the operator can override via
        # SANDBOX_SECURITY_GROUP_ID.
        gid = groups[0].get("GroupId")
        if gid:
            out["security_group_id"] = gid

    return out
