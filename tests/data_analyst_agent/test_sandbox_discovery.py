"""Unit tests for `agent_server.sandbox_discovery.discover_from_ecs_metadata`.

What we're protecting:
  - Outside ECS (no metadata URI), the function returns None — caller
    must rely on explicit env vars.
  - Inside ECS with everything reachable, every field gets populated:
    cluster_name, task_definition_arn, container_name, subnet_ids,
    security_group_id.
  - Partial failures (metadata works, boto3 fails) yield a partial dict
    rather than blowing up — the caller's `_env_or_discovered` decides
    what's a hard error per-field.
  - The reconstructed task-definition ARN uses the right shape:
    `arn:aws:ecs:<region>:<account>:task-definition/<family>:<revision>`
    where account is parsed out of the task ARN.

We never hit the network. `httpx.get` and `boto3.client` are patched in
each test, so assertions are purely about what discovery makes of the
mocked responses.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_server.sandbox_discovery import discover_from_ecs_metadata


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _clear_metadata_env(monkeypatch):
    """Each test sets ECS_CONTAINER_METADATA_URI_V4 explicitly (or doesn't)."""
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    yield


def _good_metadata() -> dict:
    """A realistic ECS task metadata v4 /task response, slimmed to what we read."""
    return {
        "Cluster": "arn:aws:ecs:eu-central-1:111222333444:cluster/my-cluster",
        "TaskARN": "arn:aws:ecs:eu-central-1:111222333444:task/my-cluster/abc123def456",
        "Family": "data-analyst-agent",
        "Revision": "7",
        "Containers": [
            {"Name": "app", "DockerName": "ecs-data-analyst-agent-7-app"},
        ],
    }


def _mock_httpx_returning(payload: dict) -> MagicMock:
    """Build a `httpx` module mock whose `.get()` returns `payload`."""
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    httpx_mod = MagicMock()
    httpx_mod.get.return_value = response
    return httpx_mod


def _mock_boto3_clients(
    *,
    eni_id: str | None = "eni-0a1b2c3d4e",
    subnet_id: str | None = "subnet-priv-1",
    sg_id: str | None = "sg-0001",
    describe_tasks_raises: bool = False,
    describe_eni_raises: bool = False,
) -> MagicMock:
    """Mock a `boto3` module with `client(...)` returning an ECS / EC2 mock.

    `eni_id`, `subnet_id`, `sg_id` can be `None` to simulate "field not
    populated in the response."
    """
    ecs_client = MagicMock()
    if describe_tasks_raises:
        ecs_client.describe_tasks.side_effect = RuntimeError("ecs boom")
    else:
        attachments = []
        if eni_id is not None:
            attachments.append(
                {
                    "type": "ElasticNetworkInterface",
                    "details": [
                        {"name": "networkInterfaceId", "value": eni_id},
                    ],
                }
            )
        ecs_client.describe_tasks.return_value = {"tasks": [{"attachments": attachments}]}

    ec2_client = MagicMock()
    if describe_eni_raises:
        ec2_client.describe_network_interfaces.side_effect = RuntimeError("ec2 boom")
    else:
        ni: dict = {}
        if subnet_id is not None:
            ni["SubnetId"] = subnet_id
        if sg_id is not None:
            ni["Groups"] = [{"GroupId": sg_id}]
        ec2_client.describe_network_interfaces.return_value = {"NetworkInterfaces": [ni]}

    def _client(service: str, **_kwargs):
        if service == "ecs":
            return ecs_client
        if service == "ec2":
            return ec2_client
        raise AssertionError(f"unexpected boto3 service: {service}")

    boto3_mod = MagicMock()
    boto3_mod.client.side_effect = _client
    return boto3_mod


# ---------------------------------------------------------------- not in ECS


def test_returns_none_when_metadata_uri_missing():
    """No ECS_CONTAINER_METADATA_URI_V4 -> the agent isn't in an ECS task."""
    assert discover_from_ecs_metadata("eu-central-1") is None


# ---------------------------------------------------------------- happy path


def test_full_discovery_when_metadata_and_boto3_both_succeed(monkeypatch):
    """All five discoverable fields populated from a healthy ECS environment."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    httpx_mod = _mock_httpx_returning(_good_metadata())
    boto3_mod = _mock_boto3_clients()

    with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
        result = discover_from_ecs_metadata("eu-central-1")

    assert result == {
        "cluster_name": "my-cluster",
        "task_definition_arn": (
            "arn:aws:ecs:eu-central-1:111222333444:task-definition/data-analyst-agent:7"
        ),
        "container_name": "app",
        "subnet_ids": ["subnet-priv-1"],
        "security_group_id": "sg-0001",
    }


def test_metadata_url_is_appended_with_slash_task(monkeypatch):
    """The function probes `<metadata_uri>/task`, not the bare URI."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    httpx_mod = _mock_httpx_returning(_good_metadata())
    boto3_mod = _mock_boto3_clients()

    with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
        discover_from_ecs_metadata("eu-central-1")

    httpx_mod.get.assert_called_once()
    url_called = httpx_mod.get.call_args.args[0]
    assert url_called.endswith("/task")


# ---------------------------------------------------------------- partial failures


def test_metadata_http_failure_returns_empty_dict(monkeypatch):
    """If httpx.get blows up, we return {} (not None), so caller knows
    we *tried* — and falls back to explicit env vars for everything."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    httpx_mod = MagicMock()
    httpx_mod.get.side_effect = RuntimeError("connection refused")
    boto3_mod = MagicMock()  # shouldn't even be reached

    with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
        result = discover_from_ecs_metadata("eu-central-1")

    assert result == {}
    boto3_mod.client.assert_not_called()


def test_describe_tasks_failure_yields_metadata_fields_only(monkeypatch):
    """ecs:DescribeTasks IAM denied (or any other boto3 failure) -> we
    still return the metadata-derived fields, just no network info."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    httpx_mod = _mock_httpx_returning(_good_metadata())
    boto3_mod = _mock_boto3_clients(describe_tasks_raises=True)

    with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
        result = discover_from_ecs_metadata("eu-central-1")

    assert "cluster_name" in result
    assert "task_definition_arn" in result
    assert "container_name" in result
    assert "subnet_ids" not in result
    assert "security_group_id" not in result


def test_describe_eni_failure_yields_subnet_and_sg_missing(monkeypatch):
    """ec2:DescribeNetworkInterfaces failure -> ENI id was found via ECS
    but we can't get its subnet/SG, so those keys are absent."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    httpx_mod = _mock_httpx_returning(_good_metadata())
    boto3_mod = _mock_boto3_clients(describe_eni_raises=True)

    with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
        result = discover_from_ecs_metadata("eu-central-1")

    assert "subnet_ids" not in result
    assert "security_group_id" not in result
    # Metadata-derived fields still present
    assert result.get("cluster_name") == "my-cluster"


def test_eni_attachment_missing_yields_subnet_and_sg_missing(monkeypatch):
    """describe_tasks succeeded but the task has no ENI attachment yet
    (race during launch) — discovery should NOT raise and should leave
    subnet/SG out of the result."""
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    httpx_mod = _mock_httpx_returning(_good_metadata())
    boto3_mod = _mock_boto3_clients(eni_id=None)

    with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
        result = discover_from_ecs_metadata("eu-central-1")

    assert "subnet_ids" not in result
    assert "security_group_id" not in result


# ---------------------------------------------------------------- shape of derived values


def test_cluster_name_is_arn_tail():
    """The metadata Cluster field is an ARN; we want just the name segment."""
    md = _good_metadata()
    md["Cluster"] = "arn:aws:ecs:eu-central-1:111222333444:cluster/Some-Cluster_Name"

    with patch.dict("os.environ", {"ECS_CONTAINER_METADATA_URI_V4": "http://x/"}):
        httpx_mod = _mock_httpx_returning(md)
        boto3_mod = _mock_boto3_clients()
        with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
            result = discover_from_ecs_metadata("eu-central-1")

    assert result.get("cluster_name") == "Some-Cluster_Name"


def test_task_definition_arn_reconstruction_uses_account_from_task_arn():
    """Family + Revision + account-from-task-ARN + region -> td ARN."""
    md = _good_metadata()
    md["Family"] = "my-app"
    md["Revision"] = "42"

    with patch.dict("os.environ", {"ECS_CONTAINER_METADATA_URI_V4": "http://x/"}):
        httpx_mod = _mock_httpx_returning(md)
        boto3_mod = _mock_boto3_clients()
        with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
            result = discover_from_ecs_metadata("us-west-2")

    assert (
        result.get("task_definition_arn")
        == "arn:aws:ecs:us-west-2:111222333444:task-definition/my-app:42"
    )


def test_first_container_name_wins():
    """When the task has multiple containers (e.g. a sidecar), we take
    the first one. Operator can override via SANDBOX_CONTAINER_NAME if wrong."""
    md = _good_metadata()
    md["Containers"] = [
        {"Name": "app"},
        {"Name": "logging-sidecar"},
    ]

    with patch.dict("os.environ", {"ECS_CONTAINER_METADATA_URI_V4": "http://x/"}):
        httpx_mod = _mock_httpx_returning(md)
        boto3_mod = _mock_boto3_clients()
        with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
            result = discover_from_ecs_metadata("eu-central-1")

    assert result.get("container_name") == "app"


def test_task_def_arn_skipped_when_revision_missing():
    """No Revision in metadata -> task_definition_arn key is absent rather
    than synthesized with a bogus revision."""
    md = _good_metadata()
    del md["Revision"]

    with patch.dict("os.environ", {"ECS_CONTAINER_METADATA_URI_V4": "http://x/"}):
        httpx_mod = _mock_httpx_returning(md)
        boto3_mod = _mock_boto3_clients()
        with patch.dict("sys.modules", {"httpx": httpx_mod, "boto3": boto3_mod}):
            result = discover_from_ecs_metadata("eu-central-1")

    assert "task_definition_arn" not in result
    # Other fields still discovered
    assert result.get("cluster_name") == "my-cluster"
