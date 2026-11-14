import os

from flask import Flask, jsonify, request

from auth import authenticate, issue_token, read_user
from db import POLICIES, RebuildError, connect, init_db, rebuild_day, set_policy

app = Flask(__name__)
app.json.ensure_ascii = False


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _user():
    return read_user(request.headers.get("Authorization"))


def _writer():
    user = _user()
    if not user:
        return None, (jsonify({"detail": "未登录"}), 401)
    if user["role"] != "writer":
        return None, (jsonify({"detail": "核对员不能动重建"}), 403)
    return user, None


def _policy_name(policy):
    return POLICIES.get(policy) if policy else None


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return jsonify({"status": "ok", "service": "gate-event-lake"})


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return jsonify({"detail": "用户名或密码错误"}), 401
    return jsonify(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/dates")
def list_dates():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.biz_date,
                   (SELECT COUNT(*) FROM ods_scan o WHERE o.biz_date = d.biz_date) AS ods_count,
                   (SELECT COUNT(*) FROM dwd_move m WHERE m.biz_date = d.biz_date) AS dwd_count,
                   s.policy,
                   (SELECT COUNT(*) FROM obsolete_layers l WHERE l.biz_date = d.biz_date)
                       AS invalidated_layers,
                   (SELECT r.appended_count FROM rebuild_runs r
                     WHERE r.biz_date = d.biz_date AND r.status = 'settled'
                     ORDER BY r.id DESC LIMIT 1) AS appended_count,
                   EXISTS(SELECT 1 FROM rebuild_runs r
                           WHERE r.biz_date = d.biz_date AND r.status = 'settled') AS built
            FROM (SELECT DISTINCT biz_date FROM ods_scan) d
            LEFT JOIN rebuild_settings s ON s.biz_date = d.biz_date
            ORDER BY d.biz_date
            """
        ).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
        row["policy_name"] = _policy_name(row["policy"])
    return jsonify(rows)


@app.get("/api/dates/<biz_date>")
def date_detail(biz_date):
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        ods = conn.execute(
            """
            SELECT id, terminal_code, raw_payload
            FROM ods_scan WHERE biz_date = %s ORDER BY id
            """,
            (biz_date,),
        ).fetchall()
        if not ods:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        moves = conn.execute(
            """
            SELECT source_id, terminal_code, container_no, event_kind, event_at
            FROM dwd_move WHERE biz_date = %s ORDER BY event_at, id
            """,
            (biz_date,),
        ).fetchall()
        settings = conn.execute(
            """
            SELECT policy, set_by, set_at FROM rebuild_settings WHERE biz_date = %s
            """,
            (biz_date,),
        ).fetchone()
        runs = conn.execute(
            """
            SELECT id, policy, status, appended_count, invalidated_count, rejected_count,
                   requested_by, created_at, settled_at
            FROM rebuild_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 10
            """,
            (biz_date,),
        ).fetchall()
        settled = next((run for run in runs if run["status"] == "settled"), None)
        rejects = []
        if settled:
            rejects = conn.execute(
                """
                SELECT source_id, reason FROM rebuild_rejects
                WHERE run_id = %s ORDER BY source_id
                """,
                (settled["id"],),
            ).fetchall()
        layer = conn.execute(
            """
            SELECT id, run_id, row_count, obsoleted_at
            FROM obsolete_layers WHERE biz_date = %s ORDER BY id DESC LIMIT 1
            """,
            (biz_date,),
        ).fetchone()
        obsolete_moves = []
        if layer:
            obsolete_moves = conn.execute(
                """
                SELECT source_id, terminal_code, container_no, event_kind, event_at
                FROM obsolete_moves WHERE layer_id = %s ORDER BY event_at, id
                """,
                (layer["id"],),
            ).fetchall()
    for move in moves:
        move["event_at"] = _stamp(move["event_at"])
    for move in obsolete_moves:
        move["event_at"] = _stamp(move["event_at"])
    for run in runs:
        run["policy_name"] = _policy_name(run["policy"])
        run["created_at"] = _stamp(run["created_at"])
        run["settled_at"] = _stamp(run["settled_at"])
    if settings:
        settings["policy_name"] = _policy_name(settings["policy"])
        settings["set_at"] = _stamp(settings["set_at"])
    obsolete = None
    if layer:
        layer["obsoleted_at"] = _stamp(layer["obsoleted_at"])
        obsolete = {"layer": layer, "moves": obsolete_moves}
    return jsonify(
        {
            "biz_date": biz_date,
            "ods": ods,
            "moves": moves,
            "settings": settings,
            "runs": runs,
            "rejects": rejects,
            "obsolete": obsolete,
        }
    )


@app.put("/api/dates/<biz_date>/settings")
def put_settings(biz_date):
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    policy = str(body.get("policy", ""))
    if policy not in POLICIES:
        return jsonify({"detail": "重建口径只能选整层重写或只补新扫描"}), 400
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        try:
            settings = set_policy(conn, biz_date, policy, user["username"])
        except RebuildError as err:
            return jsonify({"detail": err.message, "code": err.code}), 409
        conn.commit()
    settings["set_at"] = _stamp(settings["set_at"])
    settings["policy_name"] = _policy_name(settings["policy"])
    return jsonify(settings)


@app.post("/api/dates/<biz_date>/build")
def build_date(biz_date):
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    policy = str(body.get("policy", ""))
    if policy not in POLICIES:
        return jsonify({"detail": "重建口径只能选整层重写或只补新扫描"}), 400
    with connect() as conn:
        try:
            rebuild_day(conn, biz_date, policy, user["username"])
        except RebuildError as err:
            # 口径不符、重建争用已经落账，提交后页面能查到这一次。
            conn.commit()
            return jsonify({"detail": err.message, "code": err.code}), 409
        conn.commit()
    return date_detail(biz_date)


@app.get("/api/obsolete-queue")
def obsolete_queue():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT l.id AS layer_id, l.biz_date, l.row_count, l.obsoleted_at,
                   l.run_id, r.policy, r.requested_by
            FROM obsolete_layers l
            JOIN rebuild_runs r ON r.id = l.run_id
            ORDER BY l.id DESC
            """
        ).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
        row["obsoleted_at"] = _stamp(row["obsoleted_at"])
        row["policy_name"] = _policy_name(row["policy"])
    return jsonify(rows)


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
