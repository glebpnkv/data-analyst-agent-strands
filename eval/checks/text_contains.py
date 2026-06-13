"""Deterministic substring check on the agent's final answer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextContainsResult:
    score: float  # 1.0 if every required substring is present, else 0.0
    matched: list[str]
    missing: list[str]

    @property
    def passed(self) -> bool:
        return self.score >= 1.0


def check_text_contains(
    answer: str,
    expected_substrings: list[str],
    *,
    case_sensitive: bool = False,
) -> TextContainsResult:
    """Return 1.0 if `answer` contains every string in `expected_substrings`.

    Use as a coarse smoke check ("did the agent's final answer mention
    150?"). LLM-judge metrics in M3 take over the nuanced cases.
    """
    if not expected_substrings:
        return TextContainsResult(score=1.0, matched=[], missing=[])

    haystack = answer if case_sensitive else answer.lower()
    matched: list[str] = []
    missing: list[str] = []
    for needle in expected_substrings:
        target = needle if case_sensitive else needle.lower()
        (matched if target in haystack else missing).append(needle)

    score = 1.0 if not missing else 0.0
    return TextContainsResult(score=score, matched=matched, missing=missing)
