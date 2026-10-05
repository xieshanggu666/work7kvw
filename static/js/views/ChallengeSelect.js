/* 视图：社区航线挑战（挑战列表 / 发布版本化关卡 / 审核队列） */
window.ChallengeSelect = {
  name: "ChallengeSelect",
  props: ["challenges", "bodies"],
  data() {
    return {
      mode: "list",          // list | publish | review
      form: null,
      publishErr: "",
      publishing: false,
      queue: [],
      queueLoading: false,
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

    // ---------- 审核队列 ----------
    async openReview() {
      this.mode = "review";
      this.queueLoading = true;
      try {
        this.queue = (await API.reviewQueue()).submissions || [];
      } catch (e) {
        this.queue = [];
      } finally {
        this.queueLoading = false;
      }
    },
    async review(rec, action) {
      try {
        await API.reviewSubmission(rec.record_id, action, rec._note || "");
        await this.openReview();   // 刷新队列
        this.$emit("refresh");      // 联动列表：解锁状态 / 待审数 / 最佳成绩
      } catch (e) {
        alert("审核失败：" + e.message);
      }
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

    <!-- 审核队列 -->
    <div v-if="mode === 'review'" class="form-wrap">
      <div class="form-panel">
        <h2>审核队列 <span class="plan-count">通过后进入排行榜、开放回放并联动解锁</span></h2>
        <p v-if="queueLoading" class="plan-empty">加载中…</p>
        <p v-else-if="!queue.length" class="plan-empty">没有待审核的飞行记录。</p>
        <div v-for="r in queue" :key="r.record_id" class="queue-row">
          <div class="queue-main">
            <b>#{{ r.challenge_id }} {{ r.challenge_title }}</b>
            <span class="ver-tag">v{{ r.version }}</span>
            <div class="queue-meta">
              {{ r.player }} · ★{{ r.stars }} · {{ kmps(r.fuel_used) }} km/s
              · {{ Math.round(r.elapsed_days) }} 天 · {{ fmtDate(r.created_at) }}
            </div>
          </div>
          <input class="queue-note" v-model.trim="r._note" maxlength="200"
                 placeholder="审核备注（可选）">
          <button class="btn mini approve" @click="review(r, 'approve')">✓ 通过</button>
          <button class="btn mini reject" @click="review(r, 'reject')">✕ 驳回</button>
        </div>
        <div class="form-btns">
          <button class="btn ghost" @click="mode = 'list'">← 返回列表</button>
        </div>
      </div>
    </div>
  </div>`,
};
