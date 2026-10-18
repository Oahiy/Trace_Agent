"""One caller-owned, in-process task budget. Persistence belongs to the task layer."""

from contextlib import contextmanager
from dataclasses import asdict

from app.llm.contracts import AttemptRecord, ModelFailure

INPUT_LIMIT = 2800
OUTPUT_LIMIT = 1024
CONTEXT_SIZE = 4096
ESTIMATION_METHOD = "utf8-bytes-plus-template-reserve-v1; heuristic-not-tokenizer"


class CallBudget:
    def __init__(
        self, *, generation_limit: int = 4, embedding_limit: int = 32, token_limit: int = 24000
    ):
        for value, upper in [(generation_limit, 4), (embedding_limit, 32), (token_limit, 24000)]:
            if type(value) is not int or not 1 <= value <= upper:
                raise ModelFailure("invalid_model_configuration")
        self.generation_limit = generation_limit
        self.embedding_limit = embedding_limit
        self.token_limit = token_limit
        self.task_attempt = 1
        self.generation_calls = 0
        self.attempt_generation_calls = 0
        self.embedding_calls = 0
        self.generation_tokens_charged = 0
        self.embedding_tokens_charged = 0
        self.records: list[AttemptRecord] = []
        self._active = False

    @contextmanager
    def operation(self):
        if self._active:
            raise ModelFailure("budget_in_use")
        self._active = True
        try:
            yield
        finally:
            self._active = False

    def next_task_attempt(self):
        """Only the future task service may authorize a FAILED task's manual retry."""
        if self._active:
            raise ModelFailure("budget_in_use")
        if self.task_attempt >= 2:
            raise ModelFailure("attempt_budget_exhausted")
        self.task_attempt += 1
        self.attempt_generation_calls = 0

    def check(self, kind: str, output_limit: int):
        if kind == "generation":
            if self.attempt_generation_calls >= self.generation_limit:
                raise ModelFailure("call_budget_exhausted")
            if self.generation_tokens_charged + INPUT_LIMIT + output_limit > self.token_limit:
                raise ModelFailure("token_budget_exhausted")
        elif kind == "embedding":
            if self.embedding_calls >= self.embedding_limit:
                raise ModelFailure("call_budget_exhausted")
        else:
            raise ModelFailure("invalid_model_input")

    def reserve(self, kind: str, estimated_input: int, output_limit: int) -> int:
        self.check(kind, output_limit)
        if kind == "generation":
            self.generation_calls += 1
            self.attempt_generation_calls += 1
            charge = INPUT_LIMIT + output_limit
            self.generation_tokens_charged += charge
        else:
            self.embedding_calls += 1
            charge = estimated_input
            self.embedding_tokens_charged += charge
        return charge

    def settle(self, kind, reserved, estimated_input, output_limit, input_tokens, output_tokens):
        # Unknown generation usage keeps its full reservation, including failed requests.
        charge = (INPUT_LIMIT if input_tokens is None else input_tokens) + (
            output_limit if output_tokens is None else output_tokens
        )
        if kind == "generation":
            self.generation_tokens_charged += charge - reserved
        else:
            charge = estimated_input if input_tokens is None else input_tokens
            self.embedding_tokens_charged += charge - reserved
        return charge

    def snapshot(self):
        return {
            "task_attempt": self.task_attempt,
            "generation_calls": self.generation_calls,
            "attempt_generation_calls": self.attempt_generation_calls,
            "embedding_calls": self.embedding_calls,
            "generation_tokens_charged": self.generation_tokens_charged,
            "embedding_tokens_charged": self.embedding_tokens_charged,
            "records": [asdict(record) for record in self.records],
        }
