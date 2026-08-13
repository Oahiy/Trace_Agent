# Project goal

TraceAgent is an AI evidence-retrieval platform.
MedEvidence is the primary scenario.
OpsPilot is optional and must not delay MedEvidence.

# Engineering rules

- Python 3.12
- FastAPI and PostgreSQL
- Use type hints
- Keep business logic outside API route functions
- Add tests for every behavior change
- Never commit secrets or real API keys
- Prefer small vertical slices
- Do not add dependencies without explaining the reason
- Do not silently change public API contracts
- Run tests and lint before reporting completion

# Medical safety

- This system supports evidence retrieval only
- It must not provide diagnosis or treatment recommendations
- Claims must preserve source citations
- Unsupported answers should be refused

# Definition of done

A task is complete only when:
- implementation works
- tests pass
- failure cases are covered
- documentation is updated
- the change can be explained by the developer