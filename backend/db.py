import json
import os
import time

import psycopg
from psycopg.rows import dict_row

from build import clean_scan

# 重建口径只有两种：整层重写（整日明细先作废再全量收成）、
# 只补新扫描（尚未入库的贴源补进明细）。
POLICIES = {"full": "整层重写", "incremental": "只补新扫描"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ods_scan (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dwd_move (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS rebuild_settings (
    biz_date DATE PRIMARY KEY,
    policy TEXT NOT NULL CHECK (policy IN ('full', 'incremental')),
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rebuild_runs (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    policy TEXT NOT NULL,
    status TEXT NOT NULL,
    appended_count INTEGER NOT NULL DEFAULT 0,
    invalidated_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    requested_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS rebuild_rejects (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES rebuild_runs(id),
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS obsolete_layers (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES rebuild_runs(id),
    biz_date DATE NOT NULL,
    row_count INTEGER NOT NULL,
    obsoleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS obsolete_moves (
    id BIGSERIAL PRIMARY KEY,
    layer_id BIGINT NOT NULL REFERENCES obsolete_layers(id),
    source_id BIGINT NOT NULL,
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL
);
"""

LEGACY_TABLES = ("build_rejects", "build_runs")


class RebuildError(Exception):
    """重建被规则拦下。code 给接口分类，message 直接给页面看。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                _migrate_legacy(conn)
                count = conn.execute("SELECT COUNT(*) AS n FROM ods_scan").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _migrate_legacy(conn):
    """上一版的 build_runs/build_rejects 由重建模型顶替。

    已收成的业务日补一条系统落账的已收束记录，口径记整层重写，
    日列表才能接着读出作废层数与补进条数。
    """
    tables = {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    }
    if "build_runs" not in tables:
        return
    has_runs = conn.execute("SELECT 1 FROM rebuild_runs LIMIT 1").fetchone()
    if not has_runs:
        days = conn.execute(
            "SELECT biz_date, COUNT(*) AS n FROM dwd_move GROUP BY biz_date"
        ).fetchall()
        for day in days:
            conn.execute(
                """
                INSERT INTO rebuild_settings (biz_date, policy, set_by)
                VALUES (%s, 'full', 'system')
                ON CONFLICT (biz_date) DO NOTHING
                """,
                (day["biz_date"],),
            )
            conn.execute(
                """
                INSERT INTO rebuild_runs (biz_date, policy, status, appended_count, requested_by, settled_at)
                VALUES (%s, 'full', 'settled', %s, 'system', NOW())
                """,
                (day["biz_date"], day["n"]),
            )
    for table in LEGACY_TABLES:
        if table in tables:
            conn.execute(f"DROP TABLE {table}")


def _land(conn, biz_date, terminal, payload):
    conn.execute(
        """
        INSERT INTO ods_scan (biz_date, terminal_code, raw_payload)
        VALUES (%s, %s, %s::jsonb)
        """,
        (biz_date, terminal, json.dumps(payload, ensure_ascii=False)),
    )


def _seed(conn):
    built = [
        {"scan": "IN", "box": "CSNU1029384", "at": "2026-11-11T07:40:00", "gate": "T1"},
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T16:05:00", "gate": "T1"},
    ]
    waiting = [
        {"scan": "IN", "box": "TCLU5566778", "at": "2026-11-12T09:15:00", "gate": "T2"},
        {"scan": "XX", "box": "TCLU5566778", "at": "2026-11-12T11:00:00", "gate": "T2"},
        {"scan": "RS", "box": "BAD", "at": "2026-11-12T12:00:00", "gate": "T2"},
    ]
    for payload in built:
        _land(conn, "2026-11-11", payload["gate"], payload)
    for payload in waiting:
        _land(conn, "2026-11-12", payload["gate"], payload)
    set_policy(conn, "2026-11-11", "full", "admin")
    rebuild_day(conn, "2026-11-11", "full", "admin")


def _lock_day(conn, biz_date, code):
    """同一业务日的重建互斥。拿不到锁说明另一笔重建还没收束。"""
    got = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS got",
        (biz_date,),
    ).fetchone()["got"]
    if got:
        return
    if code == "unsettled":
        raise RebuildError("unsettled", "重建尚未收束，不许改重建设置")
    raise RebuildError("contention", "重建争用，后到的一次没有收成")


def set_policy(conn, biz_date, policy, username):
    """把口径写进重建设置。口径当日不得更换，重建未收束时不许动设置。"""
    if policy not in POLICIES:
        raise RebuildError("bad_policy", "重建口径只能选整层重写或只补新扫描")
    _lock_day(conn, biz_date, "unsettled")
    settings = conn.execute(
        "SELECT policy, set_by, set_at FROM rebuild_settings WHERE biz_date = %s",
        (biz_date,),
    ).fetchone()
    if settings:
        if settings["policy"] == policy:
            return settings
        same_day = conn.execute(
            "SELECT DATE(%s) = CURRENT_DATE AS same_day",
            (settings["set_at"],),
        ).fetchone()["same_day"]
        if same_day:
            raise RebuildError("policy_locked", "口径写进重建设置后，当日不得更换")
        conn.execute(
            "UPDATE rebuild_settings SET policy = %s, set_by = %s, set_at = NOW() WHERE biz_date = %s",
            (policy, username, biz_date),
        )
    else:
        conn.execute(
            "INSERT INTO rebuild_settings (biz_date, policy, set_by) VALUES (%s, %s, %s)",
            (biz_date, policy, username),
        )
    return conn.execute(
        "SELECT policy, set_by, set_at FROM rebuild_settings WHERE biz_date = %s",
        (biz_date,),
    ).fetchone()


def _record_attempt(conn, biz_date, policy, status, username):
    """没收成的尝试也落账：口径不符、重建争用都查得到。"""
    conn.execute(
        """
        INSERT INTO rebuild_runs (biz_date, policy, status, requested_by, settled_at)
        VALUES (%s, %s, %s, %s, NOW())
        """,
        (biz_date, policy, status, username),
    )


def _retire_layer(conn, run_id, biz_date):
    """整日明细先进废层队列，再从明细层撤下。返回作废条数。"""
    moves = conn.execute(
        """
        SELECT source_id, terminal_code, container_no, event_kind, event_at
        FROM dwd_move WHERE biz_date = %s ORDER BY id
        """,
        (biz_date,),
    ).fetchall()
    if not moves:
        return 0
    layer = conn.execute(
        """
        INSERT INTO obsolete_layers (run_id, biz_date, row_count)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (run_id, biz_date, len(moves)),
    ).fetchone()
    for move in moves:
        conn.execute(
            """
            INSERT INTO obsolete_moves (layer_id, source_id, terminal_code, container_no, event_kind, event_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                layer["id"],
                move["source_id"],
                move["terminal_code"],
                move["container_no"],
                move["event_kind"],
                move["event_at"],
            ),
        )
    conn.execute("DELETE FROM dwd_move WHERE biz_date = %s", (biz_date,))
    return len(moves)


def rebuild_day(conn, biz_date, req_policy, username):
    """按请求口径收成一日。请求口径必须和重建设置对得上。"""
    if req_policy not in POLICIES:
        raise RebuildError("bad_policy", "重建口径只能选整层重写或只补新扫描")
    settings = conn.execute(
        "SELECT policy FROM rebuild_settings WHERE biz_date = %s",
        (biz_date,),
    ).fetchone()
    if not settings:
        raise RebuildError("no_settings", "先定重建口径，再收成明细")
    if req_policy != settings["policy"]:
        _record_attempt(conn, biz_date, req_policy, "mismatch", username)
        raise RebuildError("mismatch", "重建口径不符，这一日不收成")
    try:
        _lock_day(conn, biz_date, "contention")
    except RebuildError:
        _record_attempt(conn, biz_date, req_policy, "contention", username)
        raise
    ods_count = conn.execute(
        "SELECT COUNT(*) AS n FROM ods_scan WHERE biz_date = %s",
        (biz_date,),
    ).fetchone()["n"]
    if req_policy == "full" and ods_count == 0:
        raise RebuildError("empty_staging", "贴源条数为零的日子不准进入废层队列")
    run = conn.execute(
        """
        INSERT INTO rebuild_runs (biz_date, policy, status, requested_by)
        VALUES (%s, %s, 'running', %s)
        RETURNING id
        """,
        (biz_date, req_policy, username),
    ).fetchone()
    invalidated = 0
    if req_policy == "full":
        invalidated = _retire_layer(conn, run["id"], biz_date)
        rows = conn.execute(
            "SELECT id, terminal_code, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
            (biz_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, terminal_code, raw_payload FROM ods_scan
            WHERE biz_date = %s
              AND id NOT IN (SELECT source_id FROM dwd_move WHERE biz_date = %s)
            ORDER BY id
            """,
            (biz_date, biz_date),
        ).fetchall()
    accepted = 0
    rejected = 0
    for row in rows:
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        cleaned, reason = clean_scan(payload)
        if reason:
            conn.execute(
                "INSERT INTO rebuild_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
                (run["id"], row["id"], reason),
            )
            rejected += 1
            continue
        conn.execute(
            """
            INSERT INTO dwd_move (biz_date, source_id, terminal_code, container_no, event_kind, event_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                biz_date,
                row["id"],
                row["terminal_code"],
                cleaned["container_no"],
                cleaned["event_kind"],
                cleaned["event_at"],
            ),
        )
        accepted += 1
    conn.execute(
        """
        UPDATE rebuild_runs
        SET status = 'settled', appended_count = %s, invalidated_count = %s,
            rejected_count = %s, settled_at = NOW()
        WHERE id = %s
        """,
        (accepted, invalidated, rejected, run["id"]),
    )
    return run["id"]
