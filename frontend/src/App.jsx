import { useEffect, useState } from "react";

const POLICY_NAMES = { full: "整层重写", incremental: "只补新扫描" };
const POLICY_HINTS = {
  full: "整日明细先作废再全量收成",
  incremental: "尚未入库的贴源补进明细",
};
const STATUS_NAMES = {
  settled: "已收束",
  running: "未收束",
  mismatch: "重建口径不符",
  contention: "重建争用",
};

function loadProfile() {
  try {
    return JSON.parse(localStorage.getItem("lake.profile") || "null");
  } catch {
    return null;
  }
}

async function request(path, options = {}) {
  const token = localStorage.getItem("lake.token") || "";
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "请求没有完成");
  }
  return data;
}

function MoveTable({ rows }) {
  return (
    <table>
      <thead>
        <tr><th>来源</th><th>箱号</th><th>事件</th><th>时间</th></tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={`${row.source_id}-${row.event_kind}-${row.event_at}`}>
            <td>{row.source_id}</td>
            <td>{row.container_no}</td>
            <td>{row.event_kind}</td>
            <td>{row.event_at}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function App() {
  const [profile, setProfile] = useState(loadProfile);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("123456");
  const [loginError, setLoginError] = useState("");
  const [dates, setDates] = useState([]);
  const [detail, setDetail] = useState(null);
  const [queue, setQueue] = useState([]);
  const [policyDraft, setPolicyDraft] = useState("full");
  const [requestPolicy, setRequestPolicy] = useState("full");
  const [formError, setFormError] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [settingsNote, setSettingsNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function openDay(bizDate) {
    const data = await request(`/api/dates/${bizDate}`);
    setDetail(data);
    setPolicyDraft(data.settings?.policy || "full");
    setRequestPolicy(data.settings?.policy || "full");
    setFormError("");
    setSettingsError("");
    setSettingsNote("");
  }

  async function refresh(prefer) {
    const [rows, obsolete] = await Promise.all([
      request("/api/dates"),
      request("/api/obsolete-queue"),
    ]);
    setDates(rows);
    setQueue(obsolete);
    const target = prefer || detail?.biz_date || rows[0]?.biz_date;
    if (target) await openDay(target);
  }

  useEffect(() => {
    if (!profile) return;
    refresh().catch(() => {
      localStorage.removeItem("lake.token");
      localStorage.removeItem("lake.profile");
      setProfile(null);
    });
  }, [profile]);

  async function submitLogin(event) {
    event.preventDefault();
    setLoginError("");
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("lake.token", data.access_token);
      const next = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("lake.profile", JSON.stringify(next));
      setProfile(next);
    } catch (error) {
      setLoginError(error.message);
    }
  }

  async function saveSettings() {
    if (!detail) return;
    setBusy(true);
    setSettingsError("");
    setSettingsNote("");
    try {
      const settings = await request(`/api/dates/${detail.biz_date}/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy: policyDraft }),
      });
      await refresh(detail.biz_date);
      setSettingsNote(`口径已写进重建设置：${settings.policy_name}，当日不得更换。`);
    } catch (error) {
      setSettingsError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function buildDay() {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    try {
      await request(`/api/dates/${detail.biz_date}/build`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy: requestPolicy }),
      });
      await refresh(detail.biz_date);
    } catch (error) {
      // 口径不符、重建争用都落了账，先刷新重建记录再亮出原因。
      await refresh(detail.biz_date).catch(() => {});
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
  }

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。收成明细之前，先给这一日定重建口径。</p>
        <form onSubmit={submitLogin}>
          <label>用户<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>口令<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <button>打开这一层</button>
          {loginError ? <p className="warn">{loginError}</p> : null}
        </form>
      </main>
    );
  }

  const isWriter = profile.role === "writer";
  const settings = detail?.settings;

  return (
    <>
      <header className="bar">
        <div>
          <p className="kicker">闸口扫描 · 分层</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <span>{profile.title} {profile.username}</span>
          <button className="line" onClick={logout}>离开</button>
        </div>
      </header>
      <main className="layout">
        <aside>
          {dates.map((row) => (
            <button
              key={row.biz_date}
              className={detail?.biz_date === row.biz_date ? "day on" : "day"}
              onClick={() => openDay(row.biz_date)}
            >
              <strong>{row.biz_date}</strong>
              <em>{row.policy_name || "未定口径"}</em>
              <span>贴源 {row.ods_count} 条 · 明细 {row.dwd_count} 条</span>
              <span>
                作废 {row.invalidated_layers} 层 · 补进 {row.built ? row.appended_count : "—"} 条
              </span>
            </button>
          ))}
        </aside>
        {detail ? (
          <section className="sheet">
            <div className="split-head">
              <h2>{detail.biz_date}</h2>
              <span className={settings ? "badge" : "badge empty"}>
                {settings ? settings.policy_name : "未定口径"}
              </span>
            </div>

            <section className="panel">
              <h3>重建设置</h3>
              {settings ? (
                <p className="note">
                  口径「{settings.policy_name}」：{POLICY_HINTS[settings.policy]}。
                  {settings.set_by} 写于 {settings.set_at}，当日不得更换。
                </p>
              ) : (
                <p className="note">这一日还没定重建口径。先写进重建设置，再收成明细。</p>
              )}
              {isWriter ? (
                <div className="row">
                  <select value={policyDraft} onChange={(event) => setPolicyDraft(event.target.value)}>
                    <option value="full">整层重写</option>
                    <option value="incremental">只补新扫描</option>
                  </select>
                  <button disabled={busy} onClick={saveSettings}>写进重建设置</button>
                </div>
              ) : (
                <p className="note">核对员不能改重建设置。</p>
              )}
              {settingsError ? <p className="warn">{settingsError}</p> : null}
              {settingsNote ? <p className="ok">{settingsNote}</p> : null}
            </section>

            <section className="panel">
              <h3>收成明细</h3>
              <div className="row">
                <select
                  value={requestPolicy}
                  disabled={!isWriter}
                  onChange={(event) => setRequestPolicy(event.target.value)}
                >
                  <option value="full">整层重写</option>
                  <option value="incremental">只补新扫描</option>
                </select>
                <button disabled={!isWriter || busy || !settings} onClick={buildDay}>
                  按请求口径收成
                </button>
              </div>
              <p className="note">
                请求口径和重建设置对不上，记重建口径不符，这一日不收成；
                两人同时对同一日动手，后到的记重建争用。
              </p>
              {formError ? <p className="warn">{formError}</p> : null}
            </section>

            <div className="layers three">
              <article>
                <h3>贴源层</h3>
                <ul>
                  {detail.ods.map((row) => (
                    <li key={row.id}>
                      <span>{row.terminal_code}</span>
                      <code>{JSON.stringify(row.raw_payload)}</code>
                    </li>
                  ))}
                </ul>
              </article>
              <article>
                <h3>作废前 · 废层</h3>
                {detail.obsolete ? (
                  <>
                    <p className="note">
                      第 {detail.obsolete.layer.id} 层 · {detail.obsolete.layer.row_count} 条 ·{" "}
                      {detail.obsolete.layer.obsoleted_at} 入队
                    </p>
                    <MoveTable rows={detail.obsolete.moves} />
                  </>
                ) : (
                  <p>这一日还没有作废的层。</p>
                )}
              </article>
              <article>
                <h3>收成后 · 明细层</h3>
                {detail.moves.length ? (
                  <MoveTable rows={detail.moves} />
                ) : (
                  <p>这一天的明细还是空的。</p>
                )}
                <h3>剔除</h3>
                <ul>
                  {detail.rejects.map((row) => (
                    <li key={row.source_id}><span>来源 {row.source_id}</span><em>{row.reason}</em></li>
                  ))}
                  {!detail.rejects.length ? <li>没有被剔除的扫描</li> : null}
                </ul>
              </article>
            </div>

            <section className="panel">
              <h3>重建记录</h3>
              {detail.runs.length ? (
                <table>
                  <thead>
                    <tr>
                      <th>时间</th><th>操作人</th><th>请求口径</th><th>结果</th>
                      <th>作废</th><th>补进</th><th>剔除</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.runs.map((run) => (
                      <tr key={run.id}>
                        <td>{run.created_at}</td>
                        <td>{run.requested_by}</td>
                        <td>{run.policy_name}</td>
                        <td>{STATUS_NAMES[run.status] || run.status}</td>
                        <td>{run.invalidated_count} 条</td>
                        <td>{run.appended_count} 条</td>
                        <td>{run.rejected_count} 条</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p>还没有重建记录。</p>
              )}
            </section>
          </section>
        ) : null}
      </main>
      <section className="sheet queue">
        <div className="split-head">
          <h2>废层队列</h2>
          <span className="note">整层重写时，作废旧层先进这里；贴源为零的日子进不来。</span>
        </div>
        {queue.length ? (
          <table>
            <thead>
              <tr><th>层号</th><th>业务日</th><th>条数</th><th>口径</th><th>操作人</th><th>入队时间</th></tr>
            </thead>
            <tbody>
              {queue.map((row) => (
                <tr key={row.layer_id} className="hop" onClick={() => openDay(row.biz_date)}>
                  <td>第 {row.layer_id} 层</td>
                  <td>{row.biz_date}</td>
                  <td>{row.row_count} 条</td>
                  <td>{row.policy_name}</td>
                  <td>{row.requested_by}</td>
                  <td>{row.obsoleted_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p>废层队列是空的。</p>
        )}
      </section>
    </>
  );
}
