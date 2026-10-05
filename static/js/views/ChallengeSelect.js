/* 视图：社区航线挑战（挑战列表 / 发布版本化关卡 / 审核队列） */
window.ChallengeSelect = {
  name: "ChallengeSelect",
  props: ["challenges", "bodies"],
  data() {
    return {
      mode: "list",          // list | publish | review
      tab: "pending",        // 审核面板：pending=初审队列 / appeals=申诉复核 / timeline=追溯
      form: null,
      publishErr: "",
      publishing: false,
      queue: [],
      queueLoading: false,
      // 审核席身份（自审/同人回避依据），本地持久化
      reviewer: {
        id: Store.get("reviewer_id") || "local-reviewer",
        name: Store.get("reviewer_name") || "本地审核席",
        role: Store.get("reviewer_role") || "reviewer",
      },
      // 申诉复核管理员身份（须与原审不同人）
      admin: {
        id: Store.get("admin_id") || "local-admin",
        name: Store.get("admin_name") || "本地管理员",
      },
      timeline: null,
      timelineLoading: false,
    };
  },
  computed: {
    bodyById() {
      const m = {};
      for (const b of this.bodies || []) m[b.id] = b;
      return m;
    },
    planetOptions() {
      return (this.bodies || []).filter(b => b.id !== "sun");
    },
    pendingTotal() {
      return (this.challenges || []).reduce((s, c) => s + (c.pending_count || 0), 0);
    },
  },
  methods: {
    kmps(v) { return (v * 1731.5).toFixed(1); },
    starOf(c) { return c.best ? c.best.stars : 0; },
    fmtDate(ts) {
      if (!ts) return "";
      const d = new Date(ts * 1000);
      const p = n => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    },
    enter(c) { if (c.unlocked) this.$emit("enter", c); },

    // ---------- 发布（新挑战 / 新版本） ----------
    blankForm() {
      return {
        mode: "create", targetId: null, targetTitle: "",
        title: "", author: Store.get("designer") || "",
        name: "", brief: "", hint: "",
        budgetKm: 8.0, tMax: 1200,
        milestones: [{ kind: "proximity", planet_id: "mars", dist: 0.18, r: 4.2, name: "" }],
        unlock: { type: "none", level_id: 1, challenge_id: null, value: 5 },
      };
    },
    openPublish(ch) {
      const f = this.blankForm();
      this.publishErr = "";
      if (!ch) { this.form = f; this.mode = "publish"; return; }
      // 发布新版本：拉取当前版本定义预填，在其上修改
      f.mode = "version"; f.targetId = ch.id; f.targetTitle = ch.title;
      this.form = f; this.mode = "publish";
      API.challenge(ch.id).then(d => {
        const v = d.current;
        f.name = "";  // 版本名留空，由设计者重新命名
        f.brief = v.brief; f.hint = v.hint;
        f.budgetKm = +(v.budget_dv * 1731.5).toFixed(1);
        f.tMax = Math.round(v.t_max);
        f.milestones = v.milestones.map(m => ({
          kind: m.kind, name: m.name,
          planet_id: m.planet_id || "mars",
          dist: m.dist || 0.18, r: m.r || 4.2,
        }));
        if (d.unlock_rule) f.unlock = { level_id: 1, value: 5, challenge_id: null, ...d.unlock_rule };
      }).catch(e => { this.publishErr = e.message; });
    },
    addMs() {
      this.form.milestones.push({ kind: "radius", planet_id: "mars", dist: 0.18, r: 4.2, name: "" });
    },
    removeMs(i) { this.form.milestones.splice(i, 1); },
    needsPlanet(kind) {
      return ["proximity", "assist_capture", "assist_then_radius"].includes(kind);
    },
    needsR(kind) {
      return ["radius", "assist_then_radius", "escape"].includes(kind);
    },
    msKindLabel(k) {
      return { proximity: "近距飞掠", radius: "半径达标", assist_capture: "引力捕获",
               assist_then_radius: "弹弓后半径", escape: "逃逸边界" }[k] || k;
    },
    async submitPublish() {
      const f = this.form;
      const def = {
        name: f.name, brief: f.brief, hint: f.hint,
        budget_dv: f.budgetKm / 1731.5,
        t_max: f.tMax,
        milestones: f.milestones.map(m => {
          const it = { kind: m.kind, name: m.name || this.msKindLabel(m.kind) };
          if (this.needsPlanet(m.kind)) it.planet_id = m.planet_id;
          if (m.kind === "proximity") it.dist = m.dist;
          if (this.needsR(m.kind)) it.r = m.r;
          return it;
        }),
      };
      const rule = f.unlock.type === "none" ? { type: "none" } : { ...f.unlock };
      this.publishing = true;
      this.publishErr = "";
      try {
        if (f.mode === "create") {
          await API.createChallenge({ title: f.title, author: f.author, ...def, unlock_rule: rule });
        } else {
          await API.publishVersion(f.targetId, { ...def, unlock_rule: rule });
        }
        Store.set("designer", f.author);
        this.mode = "list";
        this.$emit("refresh");
      } catch (e) {
        this.publishErr = e.message;
      } finally {
        this.publishing = false;
      }
    },

    // ---------- 审核 / 申诉复核 ----------
    saveReviewer() {
      Store.set("reviewer_id", this.reviewer.id);
      Store.set("reviewer_name", this.reviewer.name);
      Store.set("reviewer_role", this.reviewer.role);
    },
    saveAdmin() {
      Store.set("admin_id", this.admin.id);
      Store.set("admin_name", this.admin.name);
    },
    async openReview(tab) {
      this.tab = tab || this.tab || "pending";
      this.mode = "review";
      this.timeline = null;
      await this.loadQueue();
    },
    async switchTab(tab) {
      this.tab = tab;
      this.timeline = null;
      await this.loadQueue();
    },
    async loadQueue() {
      this.queueLoading = true;
      try {
        if (this.tab === "pending") {
          this.queue = (await API.reviewQueue()).submissions || [];
        } else if (this.tab === "appeals") {
          this.queue = (await API.appealQueue("pending")).submissions || [];
        } else if (this.tab === "ruled") {
          this.queue = (await API.appealQueue("upheld")).submissions
            .concat((await API.appealQueue("denied")).submissions);
        } else {  // report：已上榜、无在途/已决申诉的成绩，审核席可发起撤榜复核
          const all = (await API.reviewQueue("approved", 200)).submissions || [];
          this.queue = all.filter(r => !r.appeal);
        }
      } catch (e) {
        this.queue = [];
      } finally {
        this.queueLoading = false;
      }
    },
    async review(rec, action) {
      this.saveReviewer();
      try {
        await API.reviewSubmission(rec.record_id, action, rec._note || "", this.reviewer);
        await this.loadQueue();
        this.$emit("refresh");      // 联动列表：解锁状态 / 待审数 / 最佳成绩
      } catch (e) {
        alert("审核失败：" + e.message);
      }
    },
    // 审核席对终审成绩发起申诉（撤榜复核）/ 玩家侧入口在结算弹窗
    async staffAppeal(rec) {
      const reason = (rec._appealReason || "").trim();
      if (!reason) { alert("请填写申诉理由（如：轨迹数据异常的依据）。"); return; }
      this.saveReviewer();
      try {
        const appealId = (window.crypto && crypto.randomUUID)
          ? crypto.randomUUID()
          : `ap-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        await API.appealSubmission(rec.record_id, {
          reason, appeal_id: appealId,
          player_id: this.reviewer.id, player_name: this.reviewer.name,
          role: this.reviewer.role === "admin" ? "admin" : "reviewer",
        });
        await this.loadQueue();
      } catch (e) {
        alert("发起申诉失败：" + e.message);
      }
    },
    async rule(rec, decision) {
      this.saveAdmin();
      try {
        await API.ruleAppeal(rec.record_id, decision, rec._ruleNote || "",
                             { id: this.admin.id, name: this.admin.name, role: "admin" });
        await this.loadQueue();
        this.$emit("refresh");      // 翻案联动：恢复/撤榜与解锁
      } catch (e) {
        alert("复核失败：" + e.message);
      }
    },
    statusLabel(s) {
      return { pending: "待初审", approved: "已通过", rejected: "已驳回",
               revoked: "已撤榜" }[s] || s;
    },
    appealLabel(s) {
      return { pending: "待复核", upheld: "翻案成立", denied: "维持原判" }[s] || s;
    },
    async openTimeline(rec) {
      this.timelineLoading = true;
      this.timeline = { record: rec, events: [] };
      try {
        const r = await API.submissionTimeline(rec.record_id);
        this.timeline = { record: rec, events: r.events || [] };
      } catch (e) {
        alert("加载追溯链路失败：" + e.message);
        this.timeline = null;
      } finally {
        this.timelineLoading = false;
      }
    },
    eventLabel(a) {
      return { submit: "提交成绩", review: "初审", appeal: "发起申诉",
               appeal_rule: "管理员复核" }[a] || a;
    },
  },
  template: `
  <div class="screen levels-screen">
    <header class="topbar">
      <div class="logo">
        <div class="logo-mark">🌐</div>
        <div>
          <h1>社区航线挑战</h1>
          <p>设计者发布版本化关卡 · 飞行记录审核后上榜</p>
        </div>
      </div>
      <div class="topbar-right">
        <button class="btn ghost" @click="$emit('back')">← 返回关卡</button>
        <button class="btn ghost" @click="openReview">
          🗂 审核队列 <span v-if="pendingTotal" class="badge">{{ pendingTotal }}</span>
        </button>
        <button class="btn primary" @click="openPublish(null)">✎ 发布挑战</button>
      </div>
    </header>

    <!-- 挑战列表 -->
    <div v-if="mode === 'list'" class="level-grid">
      <div v-for="c in challenges" :key="c.id"
           class="level-card" :class="{ locked: !c.unlocked }"
           @click="enter(c)">
        <div class="level-num">挑战 #{{ c.id }} · v{{ c.current_version }} · {{ c.version_count }} 个版本</div>
        <h2>{{ c.title }}</h2>
        <p class="brief">{{ c.brief || '（设计者没有留下简介）' }}</p>
        <div class="best-meta">
          设计 {{ c.author }} · 预算 {{ kmps(c.budget_dv) }} km/s · {{ c.milestone_count }} 个里程碑
          <span v-if="c.pending_count" class="pending-tag">待审 {{ c.pending_count }}</span>
        </div>
        <div class="best-meta" v-if="c.best">
          最佳 {{ kmps(c.best.fuel_used) }} km/s · {{ Math.round(c.best.elapsed_days) }} 天
          <span class="replayable-tag">已上榜</span>
        </div>
        <div class="level-foot">
          <span class="stars">
            <template v-for="i in 3" :key="i">
              <span :class="i <= starOf(c) ? 'lit' : 'dim'">★</span>
            </template>
          </span>
          <span v-if="!c.unlocked" class="lock-tag">🔒 {{ c.unlock_desc }}</span>
          <span v-else class="go-btn">进入挑战 →</span>
        </div>
        <div class="card-ops">
          <button class="btn mini" @click.stop="openPublish(c)">＋ 发布新版本</button>
        </div>
      </div>
      <p v-if="!challenges.length" class="plan-empty empty-tip">
        还没有社区挑战。点击右上角「发布挑战」，设计第一条社区航线！
      </p>
    </div>

    <!-- 发布表单 -->
    <div v-if="mode === 'publish' && form" class="form-wrap">
      <div class="form-panel">
        <h2>{{ form.mode === 'create' ? '发布新挑战' : '发布新版本 · ' + form.targetTitle }}</h2>
        <p class="form-sub">每次发布生成不可变版本；成绩按版本结算排行榜，旧版本仍可回放。</p>
        <div class="form-grid">
          <template v-if="form.mode === 'create'">
            <label>挑战标题
              <input v-model.trim="form.title" maxlength="40" placeholder="例如：木星捷径">
            </label>
            <label>设计者署名
              <input v-model.trim="form.author" maxlength="24" placeholder="匿名设计者">
            </label>
          </template>
          <label>版本名（可选）
            <input v-model.trim="form.name" maxlength="40" placeholder="例如：加严预算版">
          </label>
          <label>燃料预算（km/s）
            <input type="number" v-model.number="form.budgetKm" min="0.3" max="90" step="0.1">
          </label>
          <label>时间限制（天）
            <input type="number" v-model.number="form.tMax" min="30" max="20000" step="10">
          </label>
          <label>解锁条件
            <select v-model="form.unlock.type">
              <option value="none">无（直接开放）</option>
              <option value="builtin_level">通关内置关卡</option>
              <option value="challenge">通关指定挑战</option>
              <option value="stars_total">累计星数达标</option>
            </select>
          </label>
          <label v-if="form.unlock.type === 'builtin_level'">前置内置关卡
            <select v-model.number="form.unlock.level_id">
              <option v-for="i in 5" :key="i" :value="i">第 {{ i }} 关</option>
            </select>
          </label>
          <label v-if="form.unlock.type === 'challenge'">前置挑战
            <select v-model.number="form.unlock.challenge_id">
              <option v-for="c in challenges" :key="c.id" :value="c.id"
                      :disabled="c.id === form.targetId">#{{ c.id }} {{ c.title }}</option>
            </select>
          </label>
          <label v-if="form.unlock.type === 'stars_total'">所需累计星数
            <input type="number" v-model.number="form.unlock.value" min="1" max="99">
          </label>
          <label class="span2">关卡简介
            <textarea v-model.trim="form.brief" rows="2" maxlength="500"
                      placeholder="这条航线的故事与目标…"></textarea>
          </label>
          <label class="span2">策略提示
            <textarea v-model.trim="form.hint" rows="2" maxlength="500"
                      placeholder="给玩家的一点提示（可选）…"></textarea>
          </label>
        </div>

        <h3 class="form-h3">里程碑（{{ form.milestones.length }}/5）</h3>
        <div class="ms-list">
          <div v-for="(m, i) in form.milestones" :key="i" class="ms-row">
            <select v-model="m.kind">
              <option value="proximity">近距飞掠</option>
              <option value="radius">半径达标</option>
              <option value="assist_capture">引力捕获</option>
              <option value="assist_then_radius">弹弓后半径</option>
              <option value="escape">逃逸边界</option>
            </select>
            <select v-if="needsPlanet(m.kind)" v-model="m.planet_id">
              <option v-for="p in planetOptions" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <input v-if="m.kind === 'proximity'" type="number" v-model.number="m.dist"
                   step="0.01" min="0.001" max="5" title="飞掠距离 AU" class="num">
            <input v-if="needsR(m.kind)" type="number" v-model.number="m.r"
                   step="0.1" min="0.2" max="100" title="半径 AU" class="num">
            <input class="ms-name" v-model.trim="m.name" maxlength="40"
                   :placeholder="msKindLabel(m.kind)">
            <button class="icon-btn danger" @click="removeMs(i)"
                    :disabled="form.milestones.length <= 1">✕</button>
          </div>
        </div>
        <button class="btn chip" @click="addMs" :disabled="form.milestones.length >= 5">
          + 添加里程碑
        </button>

        <p v-if="publishErr" class="form-err">⚠ {{ publishErr }}</p>
        <div class="form-btns">
          <button class="btn ghost" @click="mode = 'list'">取消</button>
          <button class="btn primary" :disabled="publishing" @click="submitPublish">
            {{ publishing ? '发布中…' : (form.mode === 'create' ? '发布挑战' : '发布新版本') }}
          </button>
        </div>
      </div>
    </div>

    <!-- 审核 / 申诉复核 -->
    <div v-if="mode === 'review'" class="form-wrap">
      <div class="form-panel">
        <h2>审核与申诉复核 <span class="plan-count">初审通过上榜 · 管理员复核可翻案并自动回滚</span></h2>
        <div class="review-tabs">
          <button :class="{ on: tab === 'pending' }" @click="switchTab('pending')">🗂 初审队列</button>
          <button :class="{ on: tab === 'appeals' }" @click="switchTab('appeals')">⚖ 申诉复核</button>
          <button :class="{ on: tab === 'report' }" @click="switchTab('report')">⛔ 上榜异议</button>
          <button :class="{ on: tab === 'ruled' }" @click="switchTab('ruled')">📜 已复核</button>
        </div>

        <!-- 初审队列 -->
        <template v-if="tab === 'pending'">
          <div class="identity-row">
            <label>审核席署名<input v-model.trim="reviewer.name" maxlength="24"
                                   @change="saveReviewer"></label>
            <label>审核员 ID<input v-model.trim="reviewer.id" maxlength="64"
                                  @change="saveReviewer"></label>
            <label>角色
              <select v-model="reviewer.role" @change="saveReviewer">
                <option value="reviewer">reviewer（初审）</option>
                <option value="admin">admin（初审+复核）</option>
              </select>
            </label>
            <span class="identity-hint">不能审核本人提交的成绩（自审回避）</span>
          </div>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!queue.length" class="plan-empty">没有待初审的飞行记录。</p>
          <div v-for="r in queue" :key="r.record_id" class="queue-row">
            <div class="queue-main">
              <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
              <span class="ver-tag">v{{ r.version }}</span>
              <span v-if="r.appeal" class="appeal-badge">申诉{{ appealLabel(r.appeal.status) }}</span>
              <div class="queue-meta">
                {{ r.player }} · ★{{ r.stars }} · {{ kmps(r.fuel_used) }} km/s
                · {{ Math.round(r.elapsed_days) }} 天 · {{ fmtDate(r.created_at) }}
              </div>
            </div>
            <input class="queue-note" v-model.trim="r._note" maxlength="200"
                   placeholder="审核备注（可选）">
            <button class="btn mini approve" @click="review(r, 'approve')">✓ 通过</button>
            <button class="btn mini reject" @click="review(r, 'reject')">✕ 驳回</button>
            <button class="btn mini ghost" @click="openTimeline(r)">链路</button>
          </div>
        </template>

        <!-- 申诉复核队列 -->
        <template v-else-if="tab === 'appeals'">
          <div class="identity-row">
            <label>复核管理员署名<input v-model.trim="admin.name" maxlength="24"
                                       @change="saveAdmin"></label>
            <label>管理员 ID<input v-model.trim="admin.id" maxlength="64"
                                  @change="saveAdmin"></label>
            <span class="identity-hint">复核须为 admin，且不能与原审审核员同人</span>
          </div>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!queue.length" class="plan-empty">没有待复核的申诉。</p>
          <div v-for="r in queue" :key="r.record_id" class="queue-row appeal-row">
            <div class="queue-main">
              <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
              <span class="ver-tag">v{{ r.version }}</span>
              <span class="status-tag" :class="r.review_status">{{ statusLabel(r.review_status) }}</span>
              <div class="queue-meta">
                {{ r.player }} · ★{{ r.stars }} · {{ kmps(r.fuel_used) }} km/s
              </div>
              <div class="appeal-reason">
                📨 {{ r.appeal.appealed_by_name }} 申诉：{{ r.appeal.reason }}
                <span class="muted">（原审：{{ r.reviewed_by_name || '历史记录' }}
                  {{ r.review_note ? '· ' + r.review_note : '' }}）</span>
              </div>
            </div>
            <input class="queue-note" v-model.trim="r._ruleNote" maxlength="200"
                   placeholder="复核结论备注（可选）">
            <button class="btn mini approve" @click="rule(r, 'upheld')">
              {{ r.review_status === 'rejected' ? '🔁 翻案恢复' : '⛔ 翻案撤榜' }}
            </button>
            <button class="btn mini reject" @click="rule(r, 'denied')">维持原判</button>
            <button class="btn mini ghost" @click="openTimeline(r)">链路</button>
          </div>
        </template>

        <!-- 上榜成绩异议（审核席发起撤榜复核） -->
        <template v-else-if="tab === 'report'">
          <p class="form-sub">对已上榜成绩有异议时，由审核席发起申诉，管理员复核成立即撤榜并回滚解锁。</p>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!queue.length" class="plan-empty">没有可发起异议的上榜成绩。</p>
          <div v-for="r in queue" :key="r.record_id" class="queue-row appeal-row">
            <div class="queue-main">
              <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
              <span class="ver-tag">v{{ r.version }}</span>
              <span class="status-tag approved">已上榜</span>
              <div class="queue-meta">
                {{ r.player }} · ★{{ r.stars }} · {{ kmps(r.fuel_used) }} km/s
                · {{ Math.round(r.elapsed_days) }} 天
              </div>
            </div>
            <input class="queue-note" v-model.trim="r._appealReason" maxlength="500"
                   placeholder="异议依据（必填，如轨迹数据异常）">
            <button class="btn mini reject" :disabled="!(r._appealReason || '').trim()"
                    @click="staffAppeal(r)">⛔ 发起撤榜复核</button>
            <button class="btn mini ghost" @click="openTimeline(r)">链路</button>
          </div>
        </template>

        <!-- 已复核 -->
        <template v-else>
          <p v-if="queueLoading" class="plan-empty">加载中…</p>
          <p v-else-if="!queue.length" class="plan-empty">还没有已复核的申诉。</p>
          <div v-for="r in queue" :key="r.record_id" class="queue-row">
            <div class="queue-main">
              <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
              <span class="ver-tag">v{{ r.version }}</span>
              <span class="status-tag" :class="r.review_status">{{ statusLabel(r.review_status) }}</span>
              <span class="appeal-badge">{{ appealLabel(r.appeal.status) }}</span>
              <div class="queue-meta">
                {{ r.player }} · 复核人 {{ r.appeal.reviewed_by_name }}
                <template v-if="r.appeal.note"> · {{ r.appeal.note }}</template>
              </div>
            </div>
            <button class="btn mini ghost" @click="openTimeline(r)">查看链路</button>
          </div>
        </template>

        <!-- 追溯链路时间线 -->
        <div v-if="timeline" class="timeline-panel">
          <h3>追溯链路 · 成绩 #{{ timeline.record.record_id }}</h3>
          <p v-if="timelineLoading" class="plan-empty">加载中…</p>
          <ol v-else class="timeline-list">
            <li v-for="e in timeline.events" :key="e.seq">
              <span class="tl-seq">{{ e.seq }}</span>
              <span class="tl-action">{{ eventLabel(e.action) }}</span>
              <span class="tl-actor">{{ e.actor_name }}（{{ e.actor_role }}）</span>
              <span class="tl-detail" v-if="e.action === 'review'">
                {{ e.detail.result === 'approved' ? '通过' : '驳回' }}
                <template v-if="e.detail.note"> · {{ e.detail.note }}</template>
              </span>
              <span class="tl-detail" v-else-if="e.action === 'appeal'">
                {{ e.detail.reason }}
              </span>
              <span class="tl-detail" v-else-if="e.action === 'appeal_rule'">
                {{ e.detail.decision === 'upheld' ? '翻案：' + e.detail.status_before + ' → ' + e.detail.status_after : '维持原判' }}
                <template v-if="e.detail.note"> · {{ e.detail.note }}</template>
              </span>
            </li>
          </ol>
          <div class="form-btns">
            <button class="btn ghost" @click="timeline = null">← 返回队列</button>
          </div>
        </div>

        <div v-if="!timeline" class="form-btns">
          <button class="btn ghost" @click="mode = 'list'">← 返回列表</button>
        </div>
      </div>
    </div>
  </div>`,
};
