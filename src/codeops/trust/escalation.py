"""Task-local decisions for requests that exceed the pre-flight scope."""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field


class EscalationRequest(BaseModel):
    boundary: Literal["local-read", "outbound", "cross-repo"]
    path: str
    reason: str


class EscalationDecision(BaseModel):
    request: EscalationRequest
    approved: bool
    reason: str
    remaining_budget: int = Field(ge=0)


class EscalationGate:
    """Account for each prompt and issue a decision scoped to one request."""

    def __init__(
        self,
        budget: int,
        approve: Callable[[EscalationRequest], bool] | None = None,
    ) -> None:
        if budget < 0:
            raise ValueError("Escalation budget must be nonnegative.")
        self.remaining_budget = budget
        self.approve = approve

    def decide(self, request: EscalationRequest) -> EscalationDecision:
        approved = False
        if self.remaining_budget <= 0:
            reason = "escalation budget exhausted"
        elif self.approve is None:
            reason = "escalation blocked in non-interactive mode"
        else:
            self.remaining_budget -= 1
            approved = self.approve(request)
            reason = "approved by user" if approved else "denied by user"
        return EscalationDecision(
            request=request,
            approved=approved,
            reason=reason,
            remaining_budget=self.remaining_budget,
        )
