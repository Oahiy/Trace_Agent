"""Cheap, cancellable dependency checks, each <1s and overall <3s; no retries."""

import asyncio
import hashlib
from datetime import UTC, datetime

import psycopg
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry


def service_scope(database_url, upload_root):
    return hashlib.sha256(f"{database_url}|{upload_root}".encode()).hexdigest()


def fresh_worker(data, name, scope, *, now=None):
    try:
        heartbeat = datetime.fromisoformat(data["last_heartbeat"].replace("Z", "+00:00"))
        age = ((now or datetime.now(UTC)) - heartbeat).total_seconds()
        return (
            0 <= age <= 10
            and data.get("state") in {"idle", "busy"}
            and name in data.get("queues", "").split(",")
            and not data.get("death")
            and data.get("traceagent_protocol") == "tasks-v2-json"
            and data.get("traceagent_scope") == scope
        )
    except (KeyError, ValueError, TypeError):
        return False


class Readiness:
    def __init__(self, database_url, upload_root, queue_settings):
        self.database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.scope = service_scope(database_url, upload_root)
        self.settings = queue_settings

    async def database(self):
        connection = None
        try:
            async with asyncio.timeout(0.8):
                connection = await psycopg.AsyncConnection.connect(
                    self.database_url,
                    autocommit=True,
                    options="-c statement_timeout=500 -c lock_timeout=100",
                )
                row = await (await connection.execute("SELECT 1")).fetchone()
                return row == (1,)
        finally:
            if connection is not None:
                await connection.close()

    async def workers(self, connection):
        key = f"rq:workers:{self.settings.name}"
        if not 1 <= await connection.scard(key) <= 32:
            return False
        keys = await connection.smembers(key)
        async with connection.pipeline(transaction=False) as pipeline:
            for worker_key in keys:
                pipeline.hgetall(worker_key)
            records = await pipeline.execute()
        return any(fresh_worker(record, self.settings.name, self.scope) for record in records)

    @staticmethod
    async def bounded(check):
        try:
            async with asyncio.timeout(0.9):
                return bool(await check())
        except Exception:
            return False

    async def ready(self):
        connection = Redis.from_url(
            self.settings.url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            retry=Retry(NoBackoff(), 0),
            max_connections=3,
        )
        try:
            async with asyncio.timeout(2.8):
                values = await asyncio.gather(
                    self.bounded(self.database),
                    self.bounded(connection.ping),
                    self.bounded(lambda: self.workers(connection)),
                )
                return all(values)
        except Exception:
            return False
        finally:
            try:
                async with asyncio.timeout(0.1):
                    await connection.aclose()
            except Exception:
                pass
