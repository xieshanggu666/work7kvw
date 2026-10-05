/* 本地存储（署名等偏好），隐私模式下静默降级 */
const Store = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};

const API = {
  async _req(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      let msg = r.statusText;
      try { const j = await r.json(); msg = j.detail || msg; } catch (e) {}
      throw new Error(msg);
    }
    return r.json();
  },
  system() { return this._req("/api/system"); },
  preview(level_id, actions) {
    return this._req("/api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level_id, actions }),
    });
  },
  run(level_id, actions) {
    return this._req("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level_id, actions }),
    });
  },
  // 提交成绩：run_id 关联服务端执行档案（可溯源），submission_id 为幂等键
  saveScore(level_id, run_id, submission_id) {
    return this._req("/api/score", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level_id, run_id, submission_id }),
    });
  },
  records(level_id) { return this._req(`/api/scores/${level_id}/records`); },
  record(record_id) { return this._req(`/api/records/${record_id}`); },

  // ---- 社区航线挑战 ----
  challenges() { return this._req("/api/challenges"); },
  createChallenge(payload) {
    return this._req("/api/challenges", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  challenge(cid) { return this._req(`/api/challenges/${cid}`); },
  publishVersion(cid, payload) {
    return this._req(`/api/challenges/${cid}/versions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  challengePreview(cid, actions, version) {
    return this._req(`/api/challenges/${cid}/preview`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actions, version: version || null }),
    });
  },
  challengeRun(cid, actions) {
    return this._req(`/api/challenges/${cid}/run`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actions }),
    });
  },
  // 提交飞行记录：run_id 关联服务端执行档案，submission_id 为幂等键
  challengeSubmit(cid, run_id, submission_id, player) {
    return this._req(`/api/challenges/${cid}/submit`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id, submission_id, player }),
    });
  },
  challengeLeaderboard(cid, version) {
    const q = version ? `?version=${version}` : "";
    return this._req(`/api/challenges/${cid}/leaderboard${q}`);
  },
  reviewQueue() { return this._req("/api/challenges/review_queue"); },
  reviewSubmission(recordId, action, note) {
    return this._req(`/api/challenges/submissions/${recordId}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, note: note || "" }),
    });
  },
  challengeSubmission(recordId) {
    return this._req(`/api/challenges/submissions/${recordId}`);
  },
};
