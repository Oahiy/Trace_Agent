"""Scenario-independent safe research failures."""

MESSAGES = {
    "idempotency_conflict": "Idempotency key is already used for different input.",
    "research_timeout": "Research exceeded its execution deadline.",
    "research_cancelled": "Research execution was cancelled.",
    "research_model_failed": "Research model request could not be completed.",
    "research_output_invalid": "Research model output did not satisfy the contract.",
    "research_budget_exhausted": "Research exhausted its request or token budget.",
    "research_context_too_large": "Evidence does not fit the research input budget.",
    "research_evidence_changed": "Research evidence changed during execution.",
    "research_failed": "Research task could not be completed.",
    "research_state_conflict": "Research state does not allow this operation.",
    "queue_wait_timeout": "Task exceeded its queue wait limit.",
}


class ResearchFailure(Exception):
    def __init__(self, code):
        self.code = code if code in MESSAGES else "research_failed"
        super().__init__(self.code)


class LeaseLost(Exception):
    """A stale execution must not write a terminal result or failure."""
