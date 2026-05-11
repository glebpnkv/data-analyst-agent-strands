"""Warm pool of single-use ECS sandbox tasks.

Each chat session claims one task at session start and releases it (=
StopTask) at session end. The pool keeps `pool_size` pre-launched, idle
tasks ready so claim() returns immediately instead of paying the
~30-60s ECS launch cost on every new session. Replacements fire on
every claim, so the pool is always heading back to `pool_size`.

Single-use semantics:
  - Tasks are NEVER returned to the pool after release.
  - No kernel-state scrub / reset logic — releasing means StopTask.
  - The `release()` API exists only to signal the pool that the agent
    is done with the task; the actual lifecycle action is destruction.

Crash recovery:
  - On `start()`, we sweep the cluster for sandbox-tagged tasks that
    aren't ours (different `AgentInstanceId`) AND are older than
    `sweep_skip_window_s`. The age guard stops a sibling agent
    restarting in parallel from accidentally reaping each other's
    fresh launches.
  - Sandbox containers also self-destruct on idle/hard-lifetime
    timeouts (`SANDBOX_IDLE_TIMEOUT_SECONDS`, see sandbox/server.py),
    so anything the pool misses gets cleaned up by ECS marking the
    task STOPPED when its entrypoint exits.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolConfig:
    """Static configuration for a SandboxPool instance.

    Values come from the agent task's environment (CDK injects them at
    deploy time), with sensible fallbacks at construction.

    `region` is required: ECS / EC2 boto3 clients refuse to construct
    without one, and we don't want a `NoRegionError` deferred to the
    first refill — fail loud at config time instead.
    """

    cluster_name: str
    task_definition_arn: str
    subnet_ids: list[str]
    security_group_id: str
    region: str
    pool_size: int = 2
    sandbox_port: int = 8081
    container_name: str = "sandbox"
    project_tag: str = "DataAnalystAgent"
    component_tag: str = "Sandbox"
    # Per-process identity. Tasks tagged with this id are "ours" and
    # exempt from orphan sweeping. Generated fresh on every agent start.
    agent_instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Per-process auth token. Injected into every RunTask via
    # `containerOverrides[].environment[]`, becomes the sandbox's
    # `SANDBOX_AUTH_TOKEN`. The SG is the actual security boundary —
    # this is belt-and-braces against misconfiguration.
    auth_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # claim() blocks at most this long waiting for a ready task.
    claim_timeout_s: float = 90.0
    # _launch_one() spends at most this long inside the
    # ECS-task-becomes-RUNNING-and-attached phase.
    launch_timeout_s: float = 180.0
    # Once we have an IP, /healthz must return 200 within this window.
    health_timeout_s: float = 30.0
    # Orphans younger than this are NOT swept — protects sibling agents
    # restarting at roughly the same time from cross-reaping.
    sweep_skip_window_s: float = 60.0


@dataclass
class ClaimedTask:
    """A sandbox task assigned to a chat session.

    Returned by `claim()` and passed back into `release()`. The agent's
    `_build_managed_agent` stores this on its `ManagedAgent.teardown`
    closure so the right ARN gets stopped when the session ends.
    """

    task_arn: str
    private_ip: str
    http_url: str
    auth_token: str


def _family_from_task_definition_arn(td_arn: str) -> str:
    """Extract the task definition family from its ARN.

    `arn:aws:ecs:eu-west-1:1234:task-definition/DataAnalystSandbox-Dev:7`
    -> `DataAnalystSandbox-Dev`. Used to scope `ListTasks` so the orphan
    sweep doesn't enumerate the whole cluster.
    """
    after_slash = td_arn.rsplit("/", 1)[-1]
    return after_slash.split(":", 1)[0]


class SandboxPool:
    def __init__(
        self,
        config: PoolConfig,
        *,
        ecs_client: Any | None = None,
        ec2_client: Any | None = None,
    ) -> None:
        self._config = config
        # Lazy boto3 clients so tests can inject mocks via the kwargs and
        # production code doesn't pay for client construction at import.
        self._ecs_client = ecs_client
        self._ec2_client = ec2_client
        # Ready, IP-assigned tasks waiting to be claimed. Use deque
        # for O(1) popleft + O(1) append. Guarded by self._lock.
        self._ready: deque[ClaimedTask] = deque()
        # Tasks currently held by a chat session. Tracked so shutdown
        # can stop them along with anything still in `_ready`.
        self._claimed: set[str] = set()
        # Notifies a waiting claim() that a task just became ready.
        self._refill_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._stopped = False
        # Track in-flight refills so shutdown can wait for them and we
        # don't over-launch when several refills race.
        self._refill_tasks: set[asyncio.Task[Any]] = set()

    # ===== boto3 client helpers ===============================================

    def _ecs(self) -> Any:
        if self._ecs_client is None:
            # Region is explicit so we don't depend on AWS_REGION /
            # AWS_DEFAULT_REGION being present in os.environ. The agent
            # task does set AWS_REGION, but relying on it broke once
            # already (see commit "Pass region explicitly to pool's
            # boto3 clients") so we make it a hard PoolConfig field.
            self._ecs_client = boto3.client("ecs", region_name=self._config.region)
        return self._ecs_client

    def _ec2(self) -> Any:
        if self._ec2_client is None:
            self._ec2_client = boto3.client("ec2", region_name=self._config.region)
        return self._ec2_client

    # ===== public API =========================================================

    async def start(self) -> None:
        """Sweep orphans, then warm the pool to `pool_size` ready tasks."""
        if self._stopped:
            raise RuntimeError("pool was already shut down")
        log.info(
            "SandboxPool starting (cluster=%s, td=%s, pool_size=%d, agent_instance_id=%s)",
            self._config.cluster_name,
            self._config.task_definition_arn,
            self._config.pool_size,
            self._config.agent_instance_id,
        )
        try:
            await self._sweep_orphans()
        except Exception as e:  # noqa: BLE001
            # Sweep failure shouldn't block startup; we'll fail loud
            # later if the cluster's full of orphans.
            log.warning("orphan sweep failed (continuing anyway): %s", e)
        for _ in range(self._config.pool_size):
            self._spawn_refill()

    async def claim(self) -> ClaimedTask:
        """Return a ready sandbox task. Block up to `claim_timeout_s`."""
        if self._stopped:
            raise RuntimeError("pool is shut down; cannot claim")

        deadline = time.monotonic() + self._config.claim_timeout_s

        while True:
            async with self._lock:
                if self._ready:
                    task = self._ready.popleft()
                    self._claimed.add(task.task_arn)
                    if not self._ready:
                        self._refill_event.clear()
                    # Single-use means we always need exactly one more in
                    # the pool to replace what we just took.
                    self._spawn_refill()
                    log.info(
                        "SandboxPool claimed %s (ready=%d, claimed=%d)",
                        task.task_arn,
                        len(self._ready),
                        len(self._claimed),
                    )
                    return task
                # Pool is empty. If there's no in-flight refill either,
                # something failed; kick a fresh launch.
                if not self._refill_tasks:
                    self._spawn_refill()
                self._refill_event.clear()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"no sandbox task ready within {self._config.claim_timeout_s:.0f}s; "
                    f"{len(self._refill_tasks)} refill(s) in flight"
                )
            try:
                await asyncio.wait_for(self._refill_event.wait(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"no sandbox task ready within {self._config.claim_timeout_s:.0f}s"
                ) from exc

    async def release(self, task: ClaimedTask) -> None:
        """Stop the task. Single-use: never returned to the pool."""
        async with self._lock:
            self._claimed.discard(task.task_arn)
        log.info("SandboxPool releasing %s", task.task_arn)
        try:
            await self._stop_task(task.task_arn, "session ended")
        except (ClientError, BotoCoreError) as e:
            # StopTask can race with the task already stopping (idle
            # timeout, etc) — log and move on. The bill stops either way.
            log.warning("StopTask %s during release raised: %s", task.task_arn, e)

    async def shutdown(self, timeout_s: float = 15.0) -> None:
        """StopTask on every idle + claimed task and wait for in-flight refills."""
        if self._stopped:
            return
        self._stopped = True
        log.info("SandboxPool shutting down")

        async with self._lock:
            ready_arns = [t.task_arn for t in self._ready]
            self._ready.clear()
            claimed_arns = list(self._claimed)
            self._claimed.clear()
            inflight = list(self._refill_tasks)

        # Cancel any refill tasks still running (their RunTask may already
        # have returned an ARN we don't know about — those will fall to
        # the next process's orphan sweep).
        for t in inflight:
            t.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)

        all_arns = ready_arns + claimed_arns
        if not all_arns:
            return
        log.info("SandboxPool stopping %d task(s) on shutdown", len(all_arns))
        results = await asyncio.gather(
            *[self._stop_task(arn, "agent shutdown") for arn in all_arns],
            return_exceptions=True,
        )
        for arn, r in zip(all_arns, results, strict=False):
            if isinstance(r, Exception):
                log.warning("StopTask %s during shutdown raised: %s", arn, r)

    # ===== refill orchestration ==============================================

    def _spawn_refill(self) -> None:
        """Schedule a `_launch_one` call as an asyncio.Task we can track.

        Caller MUST hold `self._lock` when invoking — `_refill_tasks` is
        not separately synchronized.
        """
        if self._stopped:
            return
        t = asyncio.create_task(self._refill_one())
        self._refill_tasks.add(t)
        t.add_done_callback(self._refill_tasks.discard)

    async def _refill_one(self) -> None:
        try:
            prepared = await self._launch_one()
        except Exception as e:  # noqa: BLE001
            log.exception("pool refill failed: %s", e)
            return
        async with self._lock:
            if self._stopped:
                # We launched a task while shutdown was happening — kill it.
                asyncio.create_task(self._stop_task(prepared.task_arn, "shutdown race"))
                return
            self._ready.append(prepared)
            self._refill_event.set()
            log.info(
                "SandboxPool refilled %s (ready=%d)",
                prepared.task_arn,
                len(self._ready),
            )

    # ===== single-task launch sequence =======================================

    async def _launch_one(self) -> ClaimedTask:
        """Run-task → wait running+IP → wait healthz → return ready task."""
        task_arn = await asyncio.to_thread(self._run_task_sync)
        try:
            private_ip = await self._wait_for_running(task_arn)
            http_url = f"http://{private_ip}:{self._config.sandbox_port}"
            await self._wait_for_healthz(http_url)
        except Exception:
            # Anything that fails after a successful RunTask must not leak
            # the task. Best-effort StopTask, then re-raise.
            try:
                await self._stop_task(task_arn, "launch failed")
            except Exception as stop_exc:  # noqa: BLE001
                log.warning(
                    "StopTask after failed launch raised: %s (original error not yet thrown)",
                    stop_exc,
                )
            raise
        return ClaimedTask(
            task_arn=task_arn,
            private_ip=private_ip,
            http_url=http_url,
            auth_token=self._config.auth_token,
        )

    def _run_task_sync(self) -> str:
        cfg = self._config
        # `Project`/`Component` tags pin the task as ours for the orphan
        # sweep; `AgentInstanceId` lets sibling agents distinguish each
        # other's tasks; `LaunchedAt` is the age guard for the sweep
        # race.
        tags = [
            {"key": "Project", "value": cfg.project_tag},
            {"key": "Component", "value": cfg.component_tag},
            {"key": "AgentInstanceId", "value": cfg.agent_instance_id},
            {"key": "LaunchedAt", "value": datetime.now(timezone.utc).isoformat()},
        ]
        response = self._ecs().run_task(
            cluster=cfg.cluster_name,
            taskDefinition=cfg.task_definition_arn,
            launchType="EC2",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": cfg.subnet_ids,
                    "securityGroups": [cfg.security_group_id],
                    "assignPublicIp": "DISABLED",
                },
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": cfg.container_name,
                        "environment": [
                            {"name": "SANDBOX_AUTH_TOKEN", "value": cfg.auth_token},
                        ],
                    }
                ],
            },
            tags=tags,
            # `propagateTags` is intentionally omitted: the ECS API
            # only accepts "TASK_DEFINITION" or "SERVICE", and there's
            # no "NONE" value — to keep our explicit `tags=` from being
            # merged with anything else, just don't pass the parameter.
            count=1,
        )
        failures = response.get("failures", [])
        if failures:
            raise RuntimeError(f"RunTask returned failures: {failures}")
        tasks = response.get("tasks", [])
        if not tasks:
            raise RuntimeError(f"RunTask returned no tasks: {response}")
        return tasks[0]["taskArn"]

    async def _wait_for_running(self, task_arn: str) -> str:
        """Poll DescribeTasks until RUNNING + privateIPv4Address present.

        Both conditions are required: ECS reports `lastStatus=RUNNING`
        a beat before the awsvpc ENI's IP is populated in `attachments`.
        Reading the IP at first sight of RUNNING gives an empty string
        and a downstream connection refused.
        """
        deadline = time.monotonic() + self._config.launch_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            resp = await asyncio.to_thread(
                self._ecs().describe_tasks,
                cluster=self._config.cluster_name,
                tasks=[task_arn],
            )
            tasks = resp.get("tasks", [])
            if not tasks:
                continue
            task = tasks[0]
            last_status = task.get("lastStatus")
            if last_status == "STOPPED":
                reason = task.get("stoppedReason", "<no reason>")
                raise RuntimeError(f"task {task_arn} stopped before RUNNING: {reason}")
            if last_status != "RUNNING":
                continue
            # ENI is exposed via attachments[].details[]; we want the
            # ElasticNetworkInterface attachment, then its
            # privateIPv4Address detail.
            for attachment in task.get("attachments", []):
                if attachment.get("type") != "ElasticNetworkInterface":
                    continue
                details = {d["name"]: d["value"] for d in attachment.get("details", [])}
                ip = details.get("privateIPv4Address")
                if ip:
                    return ip
            # RUNNING but no IP yet — keep polling.
        raise TimeoutError(
            f"task {task_arn} did not reach RUNNING+IP within "
            f"{self._config.launch_timeout_s:.0f}s"
        )

    async def _wait_for_healthz(self, http_url: str) -> None:
        """Poll /healthz until 200 with kernel_alive."""
        deadline = time.monotonic() + self._config.health_timeout_s
        async with httpx.AsyncClient(timeout=2.0) as client:
            last_error: str = ""
            while time.monotonic() < deadline:
                try:
                    r = await client.get(f"{http_url}/healthz")
                    if r.status_code == 200:
                        body = r.json()
                        if body.get("kernel_alive"):
                            return
                        last_error = f"kernel_alive=false ({body!r})"
                    else:
                        last_error = f"HTTP {r.status_code}"
                except Exception as e:  # noqa: BLE001
                    last_error = repr(e)
                await asyncio.sleep(1.0)
        raise TimeoutError(
            f"sandbox /healthz did not become ready within "
            f"{self._config.health_timeout_s:.0f}s; last error: {last_error}"
        )

    async def _stop_task(self, task_arn: str, reason: str) -> None:
        await asyncio.to_thread(
            self._ecs().stop_task,
            cluster=self._config.cluster_name,
            task=task_arn,
            # ECS truncates reasons over 255 chars with a 400; clamp ourselves.
            reason=reason[:255],
        )

    # ===== orphan sweep =======================================================

    async def _sweep_orphans(self) -> None:
        """Stop sandbox tasks left over from previous agent processes.

        Filtering: we only consider tasks in the sandbox task-def family
        (cheap ListTasks scope), then check tags to confirm
        Component=Sandbox AND AgentInstanceId != ours AND LaunchedAt
        is older than the skip window.
        """
        cfg = self._config
        family = _family_from_task_definition_arn(cfg.task_definition_arn)
        list_resp = await asyncio.to_thread(
            self._ecs().list_tasks,
            cluster=cfg.cluster_name,
            family=family,
            desiredStatus="RUNNING",
        )
        arns = list_resp.get("taskArns", [])
        if not arns:
            log.info("orphan sweep: no running tasks in family %s", family)
            return

        desc_resp = await asyncio.to_thread(
            self._ecs().describe_tasks,
            cluster=cfg.cluster_name,
            tasks=arns,
            include=["TAGS"],
        )
        now_utc = datetime.now(timezone.utc)
        to_stop: list[str] = []
        for task in desc_resp.get("tasks", []):
            tags = {t["key"]: t["value"] for t in task.get("tags", [])}
            if tags.get("Component") != cfg.component_tag:
                continue
            if tags.get("AgentInstanceId") == cfg.agent_instance_id:
                # Should be impossible at startup (we just generated this
                # id), but skip defensively.
                continue
            launched_at = tags.get("LaunchedAt", "")
            try:
                lt = datetime.fromisoformat(launched_at.replace("Z", "+00:00"))
                age_s = (now_utc - lt).total_seconds()
                if age_s < cfg.sweep_skip_window_s:
                    log.info(
                        "orphan sweep: skipping fresh sibling task %s (age=%.0fs)",
                        task["taskArn"], age_s,
                    )
                    continue
            except ValueError:
                # Malformed/missing LaunchedAt — old-style or hand-launched
                # task. Fall through and stop it.
                pass
            to_stop.append(task["taskArn"])

        if not to_stop:
            log.info("orphan sweep: nothing to reap")
            return
        log.info("orphan sweep: stopping %d leftover task(s): %s", len(to_stop), to_stop)
        results = await asyncio.gather(
            *[self._stop_task(arn, "orphan sweep on agent startup") for arn in to_stop],
            return_exceptions=True,
        )
        for arn, r in zip(to_stop, results, strict=False):
            if isinstance(r, Exception):
                log.warning("StopTask %s during sweep raised: %s", arn, r)
