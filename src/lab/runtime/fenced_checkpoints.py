"""Pinned PostgresSaver SQL inside one atomic run-ownership transaction."""

from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.pq import PipelineStatus, TransactionStatus
from psycopg.rows import dict_row

from lab.runner.ownership import RunClaim, check_run_cursor
from lab.runtime.serde import LosslessJsonPlusSerializer


class FencedPostgresSaver(PostgresSaver):
    def __init__(self, conn: Connection, claim: RunClaim):
        if (
            not isinstance(conn, Connection)
            or not conn.autocommit
            or conn.info.pipeline_status != PipelineStatus.OFF
        ):
            raise ValueError(
                "fenced saver requires a dedicated autocommit connection without pipeline"
            )
        if conn.info.transaction_status != TransactionStatus.IDLE:
            raise ValueError("fenced saver connection must be idle")
        super().__init__(conn, serde=LosslessJsonPlusSerializer())
        self.claim = claim

    @contextmanager
    def _cursor(self, *, pipeline: bool = False):
        # Deliberately do not enter the parent's non-reentrant lock or pipeline.
        with (
            self.lock,
            self.conn.transaction(),
            self.conn.cursor(binary=True, row_factory=dict_row) as cur,
        ):
            check_run_cursor(cur, self.claim, lock=True)
            yield cur
            check_run_cursor(cur, self.claim)

    def _thread(self, config):
        if (
            config is None
            or config.get("configurable", {}).get("thread_id") != self.claim.thread_id
        ):
            raise ValueError("checkpoint thread differs from the owned run")

    def put(self, config, checkpoint, metadata, new_versions):
        self._thread(config)
        return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        self._thread(config)
        return super().put_writes(config, writes, task_id, task_path)

    def get_tuple(self, config):
        self._thread(config)
        return super().get_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        self._thread(config)
        if before is not None:
            self._thread(before)
        return super().list(config, filter=filter, before=before, limit=limit)

    def get_delta_channel_history(self, *, config, channels):
        self._thread(config)
        return super().get_delta_channel_history(config=config, channels=channels)

    def setup(self):
        raise ValueError("workers cannot administer checkpoint tables")

    def delete_thread(self, thread_id):
        raise ValueError("workers cannot delete checkpoint history")
