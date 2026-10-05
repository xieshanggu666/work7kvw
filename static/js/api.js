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
  // player_id 为玩家稳定身份（申诉归属/自审回避），本地生成后持久化
  challengeSubmit(cid, run_id, submission_id, player, playerId) {
    return this._req(`/api/challenges/${cid}/submit`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id, submission_id, player, player_id: playerId || null }),
    });
  },
  challengeLeaderboard(cid, version) {
    const q = version ? `?version=${version}` : "";
    return this._req(`/api/challenges/${cid}/leaderboard${q}`);
  },
  reviewQueue(status) {
    const q = status ? `?status=${status}` : "";
    return this._req(`/api/challenges/review_queue${q}`);
  },
  // 初审：reviewer/admin 身份，不能审核本人成绩
  reviewSubmission(recordId, action, note, reviewer) {
    return this._req(`/api/challenges/submissions/${recordId}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action, note: note || "",
        reviewer_id: (reviewer && reviewer.id) || "local-reviewer",
        reviewer_name: (reviewer && reviewer.name) || "本地审核席",
        reviewer_role: (reviewer && reviewer.role) || "reviewer",
      }),
    });
  },
  // 申诉队列（管理员复核）
  appealQueue(status) {
    const q = status ? `?status=${status}` : "";
    return this._req(`/api/challenges/appeal_queue${q}`);
  },
  // 发起申诉：appeal_id 幂等键
  appealSubmission(recordId, payload) {
    return this._req(`/api/challenges/submissions/${recordId}/appeal`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  // 管理员复核：upheld 翻案（恢复/撤榜）/ denied 维持
  ruleAppeal(recordId, decision, note, reviewer) {
    return this._req(`/api/challenges/submissions/${recordId}/appeal/rule`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        decision, note: note || "",
        reviewer_id: (reviewer && reviewer.id) || "local-admin",
        reviewer_name: (reviewer && reviewer.name) || "本地管理员",
        reviewer_role: "admin",
      }),
    });
  },
  submissionTimeline(recordId) {
    return this._req(`/api/challenges/submissions/${recordId}/timeline`);
  },
  challengeSubmission(recordId) {
    return this._req(`/api/challenges/submissions/${recordId}`);
  },
};
