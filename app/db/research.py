"""Owner-scoped research persistence; caller controls every transaction."""

from sqlalchemy import select

from app.db.models import Job, ResearchRun


class ResearchRepository:
    def __init__(self, session):
        self.session = session

    def get(self, owner_id, job_id, *, lock=False):
        query = (
            select(Job, ResearchRun)
            .join(ResearchRun, ResearchRun.job_id == Job.id)
            .where(Job.id == job_id, Job.owner_id == owner_id, Job.operation == "research")
        )
        return self.session.execute(query.with_for_update() if lock else query).first()

    def by_key(self, owner_id, key):
        return self.session.execute(
            select(Job, ResearchRun)
            .join(ResearchRun, ResearchRun.job_id == Job.id)
            .where(
                Job.owner_id == owner_id, Job.operation == "research", Job.idempotency_key == key
            )
        ).first()

    def identity(self, execution_id):
        return self.session.execute(
            select(Job.owner_id, Job.id)
            .join(ResearchRun, ResearchRun.job_id == Job.id)
            .where(ResearchRun.execution_id == execution_id, Job.operation == "research")
        ).first()

    def unfinished(self, owner_id):
        return list(
            self.session.scalars(
                select(Job.id)
                .join(ResearchRun, ResearchRun.job_id == Job.id)
                .where(Job.owner_id == owner_id, Job.status.in_(["PENDING", "QUEUED", "RUNNING"]))
                .order_by(Job.created_at, Job.id)
                .limit(50)
            )
        )
