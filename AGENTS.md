# Project goal

Reach entry-level requirements for AI agent, LLM application, and Python backend internships within 13 weeks.

TraceAgent is the shared backend platform.
MedEvidence is the primary evidence-retrieval scenario.
OpsPilot is optional and starts only after TraceAgent passes its acceptance gate.
Cap OpsPilot at 35 hours.

Treat everything under `sources/` as read-only.
Do not edit, move, rename, or delete synced files.

# Working rules

- Prioritize the developer’s understanding and ability to work independently
- Inspect the repository and existing changes before modifying code
- Preserve unrelated work
- Prefer small vertical slices
- Avoid unnecessary refactors and dependencies
- Use official documentation for version-sensitive APIs, framework behavior, and security semantics
- Do not add dependencies without explaining the reason

For learning requests:

- Explain the design, inputs, outputs, edge cases, and test approach before showing code
- Use TraceAgent examples
- Prefer a minimal experiment over a complete solution
- End with two or three short questions about design, failure paths, and testing choices

For implementation or fix requests:

- Make the smallest complete change
- Run relevant tests
- Add a regression test for every bug fix
- Do not report unexecuted tests as passed

For diagnosis or review requests:

- Report evidence and root cause
- Do not modify code unless a fix is also requested

# Project scope

Stay focused on:

- Python
- FastAPI
- Databases
- Caching
- Task execution
- RAG
- Evaluation
- LangGraph
- Testing
- Security
- Deployment

Do not add:

- Kubernetes
- Microservices
- Multi-agent orchestration
- Complex frontends
- Model-training infrastructure
- A third business scenario

Keep shared TraceAgent components scenario-independent.
Do not hard-code MedEvidence rules into the platform layer.

# Engineering rules

- Keep API, business, and persistence responsibilities separated
- Make transaction boundaries explicit
- Make timeouts, retries, and failure behavior explicit
- Never expose secrets, tokens, stack traces, SQL, or sensitive patient information
- Do not silently change public API contracts
- Add tests for every behavior change

Every code review must cover:

1. Correctness
2. Error handling
3. Tests
4. Security
5. Maintainability

Tests must include at least:

- One success path
- One important edge case
- One failure path

# Medical safety

- MedEvidence supports evidence retrieval and research assistance only
- It must not provide diagnosis, prescriptions, dosage, or treatment recommendations
- Medical outputs must preserve source traceability
- Preserve query conditions, dates, limitations, and conflicting evidence
- Unsupported answers must be refused

# Definition of done

A task is complete only when:

- The requested behavior is implemented
- Relevant tests have been run
- Important failure cases are covered
- Affected documentation is updated
- Remaining risks are reported
- The developer can explain the design, failure behavior, and testing choices

# Weekly review

Weekly reviews must emphasize:

- Demonstrable deliverables
- Test and evaluation results
- Unresolved P0 and P1 issues
- Job-search evidence
- The developer’s ability to explain the work

Do not use hours studied or lines of code as the primary measure of progress.