"""Real PostgreSQL full-text/vector SQL with synthetic embeddings, not a model evaluation."""

from uuid import UUID

import pytest
from sqlalchemy import select
from test_day4_database import KEY_B, post
from test_day5_database import service as parse_service
from test_day10 import vector
from test_day10_database import api as api
from test_day10_database import database as database
from test_day10_database import owners as owners
from test_day10_database import postgres as postgres
from test_day10_database import run, service, upload

from app.db.models import Chunk, Document, User
from app.retrieval.contracts import RetrievalRequest
from scripts import day14

pytestmark = [pytest.mark.database, pytest.mark.usefixtures("postgres")]


def request(**kwargs):
    return RetrievalRequest(
        question="What registration information does this checklist request?", **kwargs
    )


def test_real_full_text_query_finds_registration_and_matches_dense_baseline(api):
    _, app, owners = api
    identifier = upload(
        api,
        data=b"General reporting background. " * 90
        + b"Registration number and registry name. " * 30,
        year=2021,
    )
    assert run(service(app).index(owners[0], identifier)).status == "READY"
    with app.state.database.sessions() as session:
        dense, lexical, timing = day14.ranked_rows(
            session, owners[0], [identifier], request(), vector()
        )
        assert dense and lexical and timing["dense_seconds"] >= 0
        assert all(d.id == identifier for c, d, score in lexical)
        assert "Registration number" in lexical[0][0].content
        assert (
            len(
                day14.fused_ids(
                    [str(c.id) for c, d, s in dense], [str(c.id) for c, d, s in lexical]
                )
            )
            <= 5
        )


def test_both_real_routes_exclude_other_owner_even_when_document_is_in_scope(api):
    client, app, owners = api
    content = b"Registration number and registry name. " * 80
    identifier = UUID(post(client, KEY_B, data=content).json()["document_id"])
    assert parse_service(app).parse(owners[1], identifier).status == "PARSED"
    assert run(service(app).index(owners[1], identifier)).status == "READY"
    with app.state.database.sessions() as session:
        dense, lexical, _ = day14.ranked_rows(session, owners[0], [identifier], request(), vector())
    assert dense == lexical == []


@pytest.mark.parametrize("reason", ["year", "unknown_year", "not_ready", "inactive"])
def test_both_routes_preserve_eligibility_filters(api, reason):
    _, app, owners = api
    identifier = upload(
        api,
        data=b"Registration number and registry name. " * 60,
        year=None if reason == "unknown_year" else 2021,
    )
    assert run(service(app).index(owners[0], identifier)).status == "READY"
    query = request(publication_year_to=2009 if reason == "year" else 2025)
    with app.state.database.sessions.begin() as session:
        if reason == "not_ready":
            session.get(Document, identifier).status = "PARSED"
        if reason == "inactive":
            session.get(User, owners[0]).active = False
    with app.state.database.sessions() as session:
        dense, lexical, _ = day14.ranked_rows(session, owners[0], [identifier], query, vector())
    assert dense == lexical == []


def test_real_query_empty_lexical_results_retain_dense_order(api):
    _, app, owners = api
    identifier = upload(api, data=b"General reporting background. " * 80)
    assert run(service(app).index(owners[0], identifier)).status == "READY"
    with app.state.database.sessions() as session:
        dense, lexical, _ = day14.ranked_rows(
            session, owners[0], [identifier], RetrievalRequest(question="zzzxxyy"), vector()
        )
    ids = [str(c.id) for c, d, score in dense]
    assert dense and not lexical
    assert day14.fused_ids(ids, []) == ids[:5]


def test_diagnostic_baseline_mismatch_is_not_silently_accepted(api, monkeypatch):
    _, app, owners = api
    identifier = upload(api)
    assert run(service(app).index(owners[0], identifier)).status == "READY"
    monkeypatch.setattr(day14.EmbeddingRepository, "search", lambda *a: [])
    with app.state.database.sessions() as session:
        with pytest.raises(day14.EvaluationRejected, match="^diagnostic_baseline_mismatch$"):
            day14.ranked_rows(session, owners[0], [identifier], request(), vector())


def test_scope_limit_rejects_instead_of_truncating_diagnostic(api, monkeypatch):
    _, app, owners = api
    identifier = upload(api, data=b"General reporting background. " * 150)
    assert run(service(app).index(owners[0], identifier)).status == "READY"
    with app.state.database.sessions() as session:
        assert len(list(session.scalars(select(Chunk)))) > 1
        monkeypatch.setitem(day14.CONFIG, "max_scope_chunks", 1)
        with pytest.raises(day14.EvaluationRejected, match="^diagnostic_scope_too_large$"):
            day14.ranked_rows(session, owners[0], [identifier], request(), vector())
