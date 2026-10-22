"""Append actual model invocations, including replayed turns and late responses."""

from uuid import uuid4

from psycopg.types.json import Jsonb

from lab.agent.model import ModelTurn
from lab.protocol.evidence import pack_evidence, pack_text
from lab.runner.ownership import RunClaim, check_run_cursor


class InvocationJournal:
    def __init__(self, conn, claim: RunClaim):
        if not conn.autocommit:
            raise ValueError("invocation journal requires an autocommit connection")
        self.conn, self.claim = conn, claim

    def start(
        self, *, turn: int, provider_model_id: str, messages: list[dict], tools: list[dict]
    ) -> str:
        iid = str(uuid4())
        with self.conn.transaction(), self.conn.cursor() as cur:
            check_run_cursor(cur, self.claim, lock=True)
            cur.execute(
                "INSERT INTO "
                "model_invocations(id,run_id,delivery_version,turn,provider_model_id,request_json) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    iid,
                    self.claim.run_id,
                    self.claim.version,
                    turn,
                    pack_text(provider_model_id),
                    Jsonb(pack_evidence({"messages": messages, "tools": tools})),
                ),
            )
            check_run_cursor(cur, self.claim)
        return iid

    def finish(self, invocation_id: str, result: ModelTurn, latency_ms: float) -> None:
        # No live-lease check here: a response already received is evidence even after
        # takeover. The predicate restricts it to this delivery's own unfinished row.
        with self.conn.transaction():
            result_cursor = self.conn.execute(
                "UPDATE model_invocations SET response_json=%s,tool_calls_json=%s,"
                "input_tokens=%s,output_tokens=%s,latency_ms=%s,finished_at=clock_timestamp() "
                "WHERE id=%s AND run_id=%s AND delivery_version=%s AND finished_at IS NULL",
                (
                    Jsonb(pack_evidence(result.model_dump(mode="json"))),
                    Jsonb(pack_evidence([c.model_dump(mode="json") for c in result.tool_calls])),
                    result.input_tokens,
                    result.output_tokens,
                    latency_ms,
                    invocation_id,
                    self.claim.run_id,
                    self.claim.version,
                ),
            )
            if result_cursor.rowcount != 1:
                raise ValueError("invocation is completed or belongs to another delivery")

    def fail(self, invocation_id: str, error_type: str, latency_ms: float) -> None:
        with self.conn.transaction():
            result = self.conn.execute(
                "UPDATE model_invocations SET error=%s,latency_ms=%s,finished_at=clock_timestamp() "
                "WHERE id=%s AND run_id=%s AND delivery_version=%s AND finished_at IS NULL",
                (
                    pack_text(error_type),
                    latency_ms,
                    invocation_id,
                    self.claim.run_id,
                    self.claim.version,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("invocation is completed or belongs to another delivery")
