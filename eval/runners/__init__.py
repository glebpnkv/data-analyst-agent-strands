"""Runner adapters. Each takes a golden, invokes an agent, returns a normalised result."""

from .run_agent import AgentRunResult, DeployedAgentClient

__all__ = ["AgentRunResult", "DeployedAgentClient"]
