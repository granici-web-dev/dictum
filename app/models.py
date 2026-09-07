"""Pydantic-схема issues.json — контракт между /decompose и publish. См. SPEC.md §5."""

from typing import Literal

from pydantic import BaseModel, Field


class Phase(BaseModel):
    n: int
    title: str
    goal: str


class Issue(BaseModel):
    id: str = Field(pattern=r"^I-\d{3}$")
    phase: int
    scope_id: str = Field(pattern=r"^S\d+$")
    area: Literal["frontend", "backend", "design", "infra", "research"]
    title: str = Field(max_length=60)
    description: str
    dod: list[str] = Field(min_length=2)
    depends_on: list[str] = Field(default_factory=list)
    estimate: Literal["S", "M", "L"]
    test_hint: str


class Deferred(BaseModel):
    scope_id: str
    reason: str


class IssuesFile(BaseModel):
    source: str
    lang: str
    phases: list[Phase]
    issues: list[Issue]
    deferred: list[Deferred] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
