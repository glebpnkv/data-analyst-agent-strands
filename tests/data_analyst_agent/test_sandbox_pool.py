"""Unit tests for `agent_server.sandbox_pool.SandboxPool`.

The pool orchestrates ECS RunTask, ECS DescribeTasks polling for the
ENI IP, a /healthz probe over HTTP, and StopTask on release. None of
those calls hit AWS in these tests — `boto3.client('ecs')` and
`boto3.client('ec2')` are replaced with `MagicMock`s, and the /healthz
probe is bypassed by patching `_wait_for_healthz` on the instance to a
no-op coroutine.

What the tests prove:
  - `start()` warms `pool_size` tasks (RunTask called the right number
    of times) and the orphan sweep StopTask's foreign-tagged tasks
    older than the skip window — but leaves fresh siblings alone.
  - `claim()` pops a ready task and synchronously schedules a refill,
    so the pool drifts back toward `pool_size` without the caller
    needing to wait.
  - `release()` calls StopTask and does NOT enqueue back into the pool
    (single-use).
  - `shutdown()` stops every idle + claimed task in parallel and
    cancels in-flight refills.

The pool's internals use asyncio extensively, so we use pytest-asyncio
in `mode=auto` would be tidier, but we declare every test with the
`@pytest.mark.asyncio` decorator to stay explicit.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_server.sandbox_pool import (
    ClaimedTask,
    PoolConfig,
    SandboxPool,
    _family_from_task_definition_arn,
)


# ---------------------------------------------------------------- helpers


def _make_pool(
    *,
    pool_size: int = 2,
    ecs_client: Any = None,
    ec2_client: Any = None,
    agent_instance_id: str = "test-instance-id",
    auth_token: str = "tok",
) -> SandboxPool:
    """Build a SandboxPool with mock boto3 clients and a fixed identity."""
    config = PoolConfig(
        cluster_name="test-cluster",
        task_definition_arn="arn:aws:ecs:eu-central-1:123:task-definition/DataAnalystSandbox-Dev:7",
        subnet_ids=["subnet-aaa", "subnet-bbb"],
        security_group_id="sg-0001",
        region="eu-central-1",
        pool_size=pool_size,
        sandbox_port=8081,
        agent_instance_id=agent_instance_id,
        auth_token=auth_token,
        # Tighten timeouts so tests don't hang if anything misbehaves.
        claim_timeout_s=5.0,
        launch_timeout_s=5.0,
        health_timeout_s=2.0,
    )
    return SandboxPool(
        config,
        ecs_client=ecs_client or MagicMock(),
        ec2_client=ec2_client or MagicMock(),
    )


def _running_task_response(task_arn: str, ip: str = "10.0.1.42") -> dict:
    """Shape a DescribeTasks response that the pool's poll loop accepts:
    lastStatus=RUNNING AND ENI attachment with privateIPv4Address."""
    return {
        "tasks": [
            {
                "taskArn": task_arn,
                "lastStatus": "RUNNING",
                "attachments": [
                    {
                        "type": "ElasticNetworkInterface",
                        "details": [
                            {"name": "networkInterfaceId", "value": "eni-xxx"},
                            {"name": "privateIPv4Address", "value": ip},
                        ],
                    }
                ],
            }
        ]
    }


def _patch_healthz_noop(pool: SandboxPool) -> None:
    """Skip the real httpx /healthz probes in unit tests.

    Two probes exist: `_wait_for_healthz` at launch (blocking until the
    sandbox answers 200) and `_probe_alive` at claim (a cheap re-check
    that the ready task is still answering). Both are bypassed here so
    tests don't need a real HTTP transport. Tests that need to simulate
    a dead-on-pop entry replace `_probe_alive` themselves after calling
    this helper.
    """
    pool._wait_for_healthz = AsyncMock(return_value=None)  # type: ignore[method-assign]
    pool._probe_alive = AsyncMock(return_value=True)  # type: ignore[method-assign]


# ---------------------------------------------------------------- pure helpers


def test_family_extraction_from_task_definition_arn():
    """The orphan sweep relies on extracting the family from the ARN
    so it can scope ListTasks. Stable shape: split on the last `/`,
    then the first `:`."""
    arn = "arn:aws:ecs:eu-central-1:1234:task-definition/DataAnalystSandbox-Dev:7"
    assert _family_from_task_definition_arn(arn) == "DataAnalystSandbox-Dev"


def test_family_extraction_handles_unrevisioned_arn():
    """ECS sometimes returns the active-revision-implicit form."""
    arn = "arn:aws:ecs:eu-central-1:1234:task-definition/MyFamily"
    assert _family_from_task_definition_arn(arn) == "MyFamily"


# ---------------------------------------------------------------- start() + warm


@pytest.mark.asyncio
async def test_start_warms_pool_to_pool_size():
    """N RunTasks are issued at start so the first N claims hit a hot
    cache, and orphan sweep runs once before any refill fires."""
    ecs = MagicMock()
    # Sweep returns no tasks.
    ecs.list_tasks.return_value = {"taskArns": []}
    # Each RunTask returns a unique task ARN.
    arns = [f"arn:aws:ecs:eu-central-1:123:task/test-cluster/{i}" for i in range(2)]
    ecs.run_task.side_effect = [{"tasks": [{"taskArn": arn}], "failures": []} for arn in arns]
    # DescribeTasks returns RUNNING with IPs immediately for each ARN.
    ecs.describe_tasks.side_effect = [_running_task_response(a) for a in arns]

    pool = _make_pool(pool_size=2, ecs_client=ecs)
    _patch_healthz_noop(pool)

    await pool.start()
    # Wait for the two refill tasks to finish (start fires them async).
    await asyncio.gather(*pool._refill_tasks, return_exceptions=True)

    assert ecs.list_tasks.call_count == 1, "orphan sweep ListTasks should fire exactly once"
    assert ecs.run_task.call_count == 2, "should warm pool_size tasks"
    assert len(pool._ready) == 2, "pool should have pool_size ready tasks after warming"


@pytest.mark.asyncio
async def test_run_task_uses_ec2_launch_type_and_injects_auth_token():
    """The RunTask call must say launchType=EC2 (we're on EC2 launch
    type, not Fargate) and inject SANDBOX_AUTH_TOKEN via
    containerOverrides — which is how the per-process secret reaches
    the sandbox container at start."""
    ecs = MagicMock()
    ecs.list_tasks.return_value = {"taskArns": []}
    arn = "arn:aws:ecs:eu-central-1:123:task/test-cluster/abc"
    ecs.run_task.return_value = {"tasks": [{"taskArn": arn}], "failures": []}
    ecs.describe_tasks.return_value = _running_task_response(arn)

    pool = _make_pool(pool_size=1, ecs_client=ecs, auth_token="MAGIC")
    _patch_healthz_noop(pool)
    await pool.start()
    await asyncio.gather(*pool._refill_tasks, return_exceptions=True)

    kwargs = ecs.run_task.call_args.kwargs
    assert kwargs["launchType"] == "EC2"
    assert kwargs["cluster"] == "test-cluster"
    overrides = kwargs["overrides"]["containerOverrides"][0]
    env_vars = {e["name"]: e["value"] for e in overrides["environment"]}
    assert env_vars["SANDBOX_AUTH_TOKEN"] == "MAGIC"
    # Tagged so the orphan sweep can reason about ownership.
    tag_dict = {t["key"]: t["value"] for t in kwargs["tags"]}
    assert tag_dict["Component"] == "Sandbox"
    assert tag_dict["AgentInstanceId"] == "test-instance-id"
    assert "LaunchedAt" in tag_dict


# ---------------------------------------------------------------- claim() + refill


@pytest.mark.asyncio
async def test_claim_pops_a_ready_task_and_kicks_a_refill():
    """Single-use semantics: claim takes one out and schedules its
    replacement immediately, so the pool keeps drifting back to N."""
    ecs = MagicMock()
    ecs.list_tasks.return_value = {"taskArns": []}
    arns = [f"arn-{i}" for i in range(3)]
    ecs.run_task.side_effect = [{"tasks": [{"taskArn": a}], "failures": []} for a in arns]
    ecs.describe_tasks.side_effect = [_running_task_response(a) for a in arns]

    pool = _make_pool(pool_size=2, ecs_client=ecs)
    _patch_healthz_noop(pool)

    await pool.start()
    await asyncio.gather(*pool._refill_tasks, return_exceptions=True)
    assert ecs.run_task.call_count == 2  # initial warm

    claimed = await pool.claim()
    # The claimed task is one of the warm ones, not a freshly-launched one.
    assert claimed.task_arn in arns[:2]
    assert claimed.private_ip == "10.0.1.42"
    assert claimed.http_url == "http://10.0.1.42:8081"
    assert claimed.auth_token == "tok"

    # Wait for the refill triggered by claim() to complete.
    await asyncio.gather(*pool._refill_tasks, return_exceptions=True)
    assert ecs.run_task.call_count == 3, "claim() must have triggered one more RunTask"


@pytest.mark.asyncio
async def test_claim_discards_dead_ready_task_and_returns_the_next_one():
    """Regression: a task can die while sitting in `_ready` (sandbox
    watchdog idle timeout, EC2 host scale-in, OOM). Previously claim()
    handed out the stale entry and the agent only learned it was dead
    on the first real HTTP call — `[Errno 113] No route to host` mid
    session. claim() now re-probes `/healthz` at pop time, discards
    anything that fails, calls StopTask best-effort, and tries the
    next entry."""
    ecs = MagicMock()
    ecs.list_tasks.return_value = {"taskArns": []}
    arns = [f"arn-{i}" for i in range(3)]
    ecs.run_task.side_effect = [{"tasks": [{"taskArn": a}], "failures": []} for a in arns]
    ecs.describe_tasks.side_effect = [_running_task_response(a) for a in arns]

    pool = _make_pool(pool_size=2, ecs_client=ecs)
    _patch_healthz_noop(pool)

    await pool.start()
    await asyncio.gather(*pool._refill_tasks, return_exceptions=True)
    assert len(pool._ready) == 2

    # First ready entry is dead, second is alive. claim() must skip the
    # dead one and return the live one.
    pool._probe_alive = AsyncMock(side_effect=[False, True])  # type: ignore[method-assign]

    claimed = await pool.claim()

    # The returned task is the SECOND ready entry — the first was discarded.
    assert claimed.task_arn == arns[1]
    # The discarded task got a best-effort StopTask.
    stop_reasons = [c.kwargs.get("reason", "") for c in ecs.stop_task.call_args_list]
    assert any("claim-time healthz" in r for r in stop_reasons), (
        f"discarded task should have been stopped with a claim-time reason; got {stop_reasons!r}"
    )
    # The discarded ARN must not appear in `_claimed` — only the live one.
    assert pool._claimed == {arns[1]}


@pytest.mark.asyncio
async def test_claim_times_out_when_pool_cant_warm():
    """If RunTask keeps failing, claim() shouldn't hang forever."""
    ecs = MagicMock()
    ecs.list_tasks.return_value = {"taskArns": []}
    ecs.run_task.side_effect = RuntimeError("fake throttle")

    pool = _make_pool(pool_size=2, ecs_client=ecs)
    _patch_healthz_noop(pool)
    # claim_timeout_s=5.0 set in _make_pool; bring it down further so
    # the test runs fast.
    object.__setattr__(pool._config, "claim_timeout_s", 0.5)

    await pool.start()
    with pytest.raises(TimeoutError, match="no sandbox task ready"):
        await pool.claim()


# ---------------------------------------------------------------- release()


@pytest.mark.asyncio
async def test_release_calls_stop_task_and_does_not_recycle():
    """The whole point of single-use: release must NOT put the task
    back in the ready deque."""
    ecs = MagicMock()
    pool = _make_pool(ecs_client=ecs)
    claimed = ClaimedTask(
        task_arn="arn-released",
        private_ip="10.0.1.5",
        http_url="http://10.0.1.5:8081",
        auth_token="tok",
    )
    # Pretend we'd issued this claim earlier.
    pool._claimed.add(claimed.task_arn)

    await pool.release(claimed)

    ecs.stop_task.assert_called_once()
    kwargs = ecs.stop_task.call_args.kwargs
    assert kwargs["task"] == "arn-released"
    assert kwargs["cluster"] == "test-cluster"
    assert kwargs["reason"]  # non-empty; ECS truncates >255 chars but we clamp.

    assert claimed.task_arn not in pool._claimed, "release() must clear the claim"
    assert all(t.task_arn != claimed.task_arn for t in pool._ready), \
        "release() must NOT put the task back in the ready deque"


# ---------------------------------------------------------------- shutdown()


@pytest.mark.asyncio
async def test_shutdown_stops_idle_and_claimed_tasks():
    """All tracked tasks (idle and claimed) get StopTask'd. Refills
    don't fire after shutdown; whatever was in flight is cancelled."""
    ecs = MagicMock()
    pool = _make_pool(ecs_client=ecs)

    # Fake state: 1 idle, 1 claimed.
    pool._ready.append(
        ClaimedTask(
            task_arn="arn-idle",
            private_ip="10.0.1.1",
            http_url="http://10.0.1.1:8081",
            auth_token="tok",
        )
    )
    pool._claimed.add("arn-claimed")

    await pool.shutdown()

    stopped_arns = [c.kwargs["task"] for c in ecs.stop_task.call_args_list]
    assert sorted(stopped_arns) == ["arn-claimed", "arn-idle"]
    assert pool._stopped is True


# ---------------------------------------------------------------- orphan sweep


@pytest.mark.asyncio
async def test_orphan_sweep_stops_old_foreign_tasks_only():
    """Sweep target: tasks tagged Component=Sandbox AND
    AgentInstanceId != ours AND LaunchedAt < now - skip_window.

    We populate three candidate orphans to exercise each branch of the
    filter:
      - foreign + old      -> stopped
      - foreign + fresh    -> skipped (sibling-restart guard)
      - ours               -> skipped
    """
    ecs = MagicMock()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=10)).isoformat()
    fresh = (now - timedelta(seconds=5)).isoformat()

    ecs.list_tasks.return_value = {
        "taskArns": ["arn-old-foreign", "arn-fresh-foreign", "arn-ours"]
    }
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn-old-foreign",
                "tags": [
                    {"key": "Component", "value": "Sandbox"},
                    {"key": "AgentInstanceId", "value": "other-agent"},
                    {"key": "LaunchedAt", "value": old},
                ],
            },
            {
                "taskArn": "arn-fresh-foreign",
                "tags": [
                    {"key": "Component", "value": "Sandbox"},
                    {"key": "AgentInstanceId", "value": "another-other-agent"},
                    {"key": "LaunchedAt", "value": fresh},
                ],
            },
            {
                "taskArn": "arn-ours",
                "tags": [
                    {"key": "Component", "value": "Sandbox"},
                    {"key": "AgentInstanceId", "value": "test-instance-id"},
                    {"key": "LaunchedAt", "value": old},
                ],
            },
        ]
    }

    pool = _make_pool(pool_size=0, ecs_client=ecs, agent_instance_id="test-instance-id")
    await pool._sweep_orphans()

    stopped_arns = [c.kwargs["task"] for c in ecs.stop_task.call_args_list]
    assert stopped_arns == ["arn-old-foreign"]


@pytest.mark.asyncio
async def test_orphan_sweep_handles_empty_cluster_silently():
    """No tasks => no DescribeTasks call, no StopTask, no error."""
    ecs = MagicMock()
    ecs.list_tasks.return_value = {"taskArns": []}
    pool = _make_pool(ecs_client=ecs)
    await pool._sweep_orphans()
    ecs.describe_tasks.assert_not_called()
    ecs.stop_task.assert_not_called()


# ---------------------------------------------------------------- launch failure


@pytest.mark.asyncio
async def test_launch_one_stops_task_if_health_check_fails():
    """Once RunTask succeeds, any later failure (RUNNING-poll timeout,
    /healthz timeout) MUST StopTask the leaked task — otherwise
    refills race ahead and we leak compute. The unit test exercises
    the /healthz failure branch."""
    ecs = MagicMock()
    arn = "arn-leaky"
    ecs.run_task.return_value = {"tasks": [{"taskArn": arn}], "failures": []}
    ecs.describe_tasks.return_value = _running_task_response(arn)

    pool = _make_pool(pool_size=1, ecs_client=ecs)
    pool._wait_for_healthz = AsyncMock(  # type: ignore[method-assign]
        side_effect=TimeoutError("healthz never returned 200")
    )

    with pytest.raises(TimeoutError):
        await pool._launch_one()

    ecs.stop_task.assert_called_once()
    assert ecs.stop_task.call_args.kwargs["task"] == arn
