<script setup>
import { computed, ref } from "vue";

const token = ref(localStorage.getItem("tide.token") || "");
const profile = ref(JSON.parse(localStorage.getItem("tide.profile") || "null"));
const username = ref("admin");
const password = ref("123456");
const loginError = ref("");
const batches = ref([]);
const current = ref(null);
const title = ref("");
const rawText = ref("");
const formError = ref("");
const busy = ref(false);
const tab = ref("desk");

const settings = ref(null);
const settingError = ref("");
const settingBusy = ref(false);
const chosenPolicy = ref("rollback_all");

const events = ref([]);
const currentEvent = ref(null);
const rejects = ref([]);

const canWrite = computed(() => profile.value && profile.value.role === "writer");

const TABS = [
  { key: "desk", label: "归集台" },
  { key: "rollbacks", label: "回滚列表" },
  { key: "policy", label: "回滚设置" },
  { key: "rejects", label: "未接纳队列" },
];

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token.value) headers.Authorization = `Bearer ${token.value}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    if (detail && typeof detail === "object") {
      throw new Error(detail.message || "请求没有完成");
    }
    throw new Error(typeof detail === "string" ? detail : "请求没有完成");
  }
  return data;
}

async function refresh() {
  batches.value = await request("/api/batches");
  if (!current.value && batches.value.length) {
    await openBatch(batches.value[0].id);
  } else if (current.value) {
    await openBatch(current.value.batch.id);
  }
}

async function openBatch(id) {
  current.value = await request(`/api/batches/${id}`);
}

async function openTab(key) {
  tab.value = key;
  if (key === "rollbacks") await loadEvents();
  if (key === "policy") await loadSettings();
  if (key === "rejects") rejects.value = await request("/api/rejects");
}

async function loadSettings() {
  settings.value = await request("/api/rollback/settings");
  if (settings.value.today) chosenPolicy.value = settings.value.today.closing_policy;
}

async function submitSetting() {
  settingError.value = "";
  settingBusy.value = true;
  try {
    await request("/api/rollback/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ closing_policy: chosenPolicy.value }),
    });
    await loadSettings();
  } catch (error) {
    settingError.value = error.message;
  } finally {
    settingBusy.value = false;
  }
}

async function loadEvents() {
  events.value = await request("/api/rollback/events");
  if (!currentEvent.value && events.value.length) {
    await openEvent(events.value[0].id);
  }
}

async function openEvent(id) {
  currentEvent.value = await request(`/api/rollback/events/${id}`);
}

async function submitLogin() {
  loginError.value = "";
  try {
    const data = await request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    token.value = data.access_token;
    profile.value = { username: data.username, role: data.role, title: data.title };
    localStorage.setItem("tide.token", token.value);
    localStorage.setItem("tide.profile", JSON.stringify(profile.value));
    await refresh();
  } catch (error) {
    loginError.value = error.message;
  }
}

async function submitBatch() {
  formError.value = "";
  busy.value = true;
  try {
    const data = await request("/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title.value, raw_text: rawText.value }),
    });
    title.value = "";
    rawText.value = "";
    await refresh();
    current.value = data;
  } catch (error) {
    formError.value = error.message;
    await refresh();
  } finally {
    busy.value = false;
  }
}

function logout() {
  token.value = "";
  profile.value = null;
  current.value = null;
  settings.value = null;
  events.value = [];
  currentEvent.value = null;
  rejects.value = [];
  localStorage.removeItem("tide.token");
  localStorage.removeItem("tide.profile");
}

if (token.value) {
  refresh().catch(() => logout());
}
</script>

<template>
  <header class="top">
    <div>
      <p class="eyebrow">水文组 · 报文归集</p>
      <h1>潮位报文归集台</h1>
    </div>
    <div v-if="profile" class="who">
      <span>{{ profile.title }} {{ profile.username }}</span>
      <button class="ghost" @click="logout">离开</button>
    </div>
  </header>

  <main v-if="!profile" class="gate">
    <form @submit.prevent="submitLogin">
      <p>站码、时刻、潮位厘米、质量旗。一行一条，竖线分开。</p>
      <label>用户<input v-model="username" autocomplete="username" /></label>
      <label>口令<input v-model="password" type="password" autocomplete="current-password" /></label>
      <button>进入归集台</button>
      <p v-if="loginError" class="warn">{{ loginError }}</p>
    </form>
  </main>

  <template v-else>
    <nav class="tabs">
      <button
        v-for="item in TABS"
        :key="item.key"
        class="tab"
        :class="{ on: tab === item.key }"
        @click="openTab(item.key)"
      >
        {{ item.label }}
      </button>
    </nav>

    <!-- 归集台：贴报文 + 批次列表 + 批次详情 -->
    <main v-if="tab === 'desk'" class="desk">
      <section class="paste">
        <h2>贴入这一批</h2>
        <p class="rule">
          坏行怎么收口由当日回滚设置定死：整批收回，或坏行进未接纳、其余照常入库。空行不算坏行。
        </p>
        <label>批次名<input v-model="title" :disabled="!canWrite" placeholder="例如 堡镇 11-13 午班" /></label>
        <textarea
          v-model="rawText"
          :disabled="!canWrite"
          placeholder="BZ03|202611130900|176|A"
          spellcheck="false"
        />
        <button :disabled="!canWrite || busy" @click="submitBatch">归集这一批</button>
        <p v-if="!canWrite" class="quiet">档案员只能翻已落入的报文。</p>
        <p v-if="formError" class="warn">{{ formError }}</p>
      </section>

      <section class="slips">
        <h2>已收批次</h2>
        <button
          v-for="batch in batches"
          :key="batch.id"
          class="slip"
          :class="{ on: current && current.batch.id === batch.id }"
          @click="openBatch(batch.id)"
        >
          <strong>{{ batch.title }}</strong>
          <em>{{ batch.status }}</em>
          <span>落入 {{ batch.accepted_count }} 行 · 未接纳 {{ batch.rejected_count }} 行</span>
          <span v-if="batch.closing_label">当日收口：{{ batch.closing_label }}</span>
          <span v-if="batch.recovered_count">收回 {{ batch.recovered_count }} 行 · 留下 {{ batch.kept_count }} 行</span>
        </button>

        <article v-if="current" class="tape">
          <header>
            <h3>{{ current.batch.title }}</h3>
            <span :class="['badge', current.batch.status]">{{ current.batch.status }}</span>
          </header>
          <p class="quiet">
            收口：{{ current.batch.closing_label || "未记录" }}
            <template v-if="current.batch.recovered_count">
              ｜收回 {{ current.batch.recovered_count }} 行 · 留下 {{ current.batch.kept_count }} 行
            </template>
          </p>

          <h4>本批收口记录</h4>
          <ul v-if="current.events.length" class="eventlist">
            <li v-for="event in current.events" :key="event.id">
              <strong>{{ event.event_label }}</strong>
              <span>收回 {{ event.recovered_count }} 行 · 留下 {{ event.kept_count }} 行 · 收口 {{ event.closing_label }}</span>
              <span class="quiet">{{ event.created_at }} · {{ event.created_by }}</span>
              <mark v-if="event.mismatch" class="mismatch">{{ event.mismatch_note }}</mark>
            </li>
          </ul>
          <p v-else class="quiet">这一批没有坏行，没有触发收口。</p>

          <h4>当前落库行（回滚后留下的）</h4>
          <table>
            <thead>
              <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in current.landed" :key="row.line_no">
                <td>{{ row.line_no }}</td>
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td>{{ row.tide_cm }}</td>
                <td>{{ row.flag_label }}</td>
              </tr>
              <tr v-if="!current.landed.length"><td colspan="5">这一批没有留在库里的行</td></tr>
            </tbody>
          </table>

          <h4 v-if="current.withdrawn.length">整批收回的行（回滚前曾落库）</h4>
          <table v-if="current.withdrawn.length">
            <thead>
              <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in current.withdrawn" :key="row.event_id + '-' + row.line_no" class="withdrawn">
                <td>{{ row.line_no }}</td>
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td>{{ row.tide_cm }}</td>
                <td>{{ row.flag_label }}</td>
              </tr>
            </tbody>
          </table>

          <h4>未接纳</h4>
          <ul>
            <li v-for="row in current.rejected" :key="row.line_no">
              <code>{{ row.raw_line }}</code>
              <span>{{ row.reason }}</span>
            </li>
            <li v-if="!current.rejected.length">没有被挡下的行</li>
          </ul>
        </article>
      </section>
    </main>

    <!-- 回滚列表 + 详情 -->
    <main v-else-if="tab === 'rollbacks'" class="rollback-page">
      <section class="panel">
        <h2>回滚列表</h2>
        <p class="quiet">每条写明收回行数、留下行数和当时的收口名；争用只记录，不做回滚。</p>
        <button
          v-for="event in events"
          :key="event.id"
          class="event-card"
          :class="{ on: currentEvent && currentEvent.event.id === event.id }"
          @click="openEvent(event.id)"
        >
          <strong>#{{ event.id }} {{ event.event_label }} · {{ event.batch_title }}</strong>
          <span>收回 {{ event.recovered_count }} 行 ／ 留下 {{ event.kept_count }} 行</span>
          <span>收口名：{{ event.closing_label || "—" }}</span>
          <span class="quiet">{{ event.created_at }} · {{ event.created_by }}</span>
          <mark v-if="event.mismatch" class="mismatch">{{ event.mismatch_note }}</mark>
        </button>
        <p v-if="!events.length" class="quiet">还没有回滚或争用记录。</p>
      </section>

      <section v-if="currentEvent" class="panel">
        <h2>回滚详情 · 批次 {{ currentEvent.event.batch_title }}</h2>
        <header class="detail-head">
          <span :class="['badge', currentEvent.event.event_type]">{{ currentEvent.event.event_label }}</span>
          <mark v-if="currentEvent.event.mismatch" class="mismatch">{{ currentEvent.event.mismatch_note }}</mark>
        </header>
        <p class="quiet">{{ currentEvent.event.note }}</p>
        <div class="diff">
          <div>
            <span>回滚前落库</span>
            <strong>{{ currentEvent.landed_before }}</strong>
          </div>
          <div class="arrow">→</div>
          <div>
            <span>回滚后留下</span>
            <strong>{{ currentEvent.landed_after }}</strong>
          </div>
          <div class="delta">
            <span>落库差（收回）</span>
            <strong>-{{ currentEvent.landed_diff }}</strong>
          </div>
        </div>
        <h4>收回行快照</h4>
        <table>
          <thead>
            <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in currentEvent.withdrawn" :key="row.line_no" class="withdrawn">
              <td>{{ row.line_no }}</td>
              <td>{{ row.station_code }}</td>
              <td>{{ row.observed_at }}</td>
              <td>{{ row.tide_cm }}</td>
              <td>{{ row.flag_label }}</td>
            </tr>
            <tr v-if="!currentEvent.withdrawn.length"><td colspan="5">没有收回行（逐行留下或争用不收回）</td></tr>
          </tbody>
        </table>
      </section>
    </main>

    <!-- 回滚设置（策略页） -->
    <main v-else-if="tab === 'policy'" class="policy-page">
      <section class="panel">
        <h2>回滚设置 · 当日收口</h2>
        <p class="rule">
          收口写进本页后，本日不得更换；回滚还没落地时，本页设置也不准再碰。
        </p>

        <div v-if="settings && settings.today" class="locked">
          <p>今日收口已定死：<strong>{{ settings.today.closing_label }}</strong></p>
          <p class="quiet">
            {{ settings.today.setting_date }} 由 {{ settings.today.locked_by }}
            于 {{ settings.today.locked_at }} 落定，本日不得更换。
          </p>
        </div>

        <form v-else @submit.prevent="submitSetting" class="policy-form">
          <label v-for="option in settings ? settings.options : []" :key="option.value" class="policy-option">
            <input type="radio" name="policy" :value="option.value" v-model="chosenPolicy" :disabled="!canWrite" />
            <span>
              <strong>{{ option.label }}</strong>
              <small v-if="option.value === 'rollback_all'">坏行出现后，已落库的行整批回滚</small>
              <small v-else>坏行单独进未接纳队列，其余行照常入库</small>
            </span>
          </label>
          <button :disabled="!canWrite || settingBusy">定死今日收口</button>
          <p v-if="!canWrite" class="quiet">档案员只能查看，不能落收口。</p>
        </form>

        <p v-if="settingError" class="warn">{{ settingError }}</p>

        <div v-if="settings && settings.effective" class="effective">
          <h4>当前生效收口</h4>
          <p>{{ settings.effective.closing_label }}（{{ settings.effective.setting_date }} 落定）</p>
        </div>
      </section>
    </main>

    <!-- 未接纳队列 -->
    <main v-else-if="tab === 'rejects'" class="reject-page">
      <section class="panel">
        <h2>未接纳队列</h2>
        <p class="quiet">逐行留下时坏行进这里；整批收回的批次，坏行同样留在此处待处理。</p>
        <table>
          <thead>
            <tr><th>批次</th><th>行</th><th>原文</th><th>挡下原因</th><th>当时收口</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, idx) in rejects" :key="row.batch_id + '-' + row.line_no + '-' + idx">
              <td>{{ row.batch_title }}</td>
              <td>{{ row.line_no }}</td>
              <td><code>{{ row.raw_line }}</code></td>
              <td>{{ row.reason }}</td>
              <td>{{ row.closing_label || "—" }}</td>
            </tr>
            <tr v-if="!rejects.length"><td colspan="5">队列是空的</td></tr>
          </tbody>
        </table>
      </section>
    </main>
  </template>
</template>

<style scoped>
.top, .desk, .gate, .rollback-page, .policy-page, .reject-page, .tabs {
  width: min(1180px, calc(100% - 32px));
  margin: 0 auto;
}
.top { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 12px; }
.eyebrow { letter-spacing: 0.18em; text-transform: uppercase; color: #d7a15f; font-size: 12px; margin: 0 0 6px; }
h1 { margin: 0; font-weight: 560; font-size: 34px; }
h2, h3, h4 { margin: 0; }
.who { display: flex; gap: 12px; align-items: center; }
.ghost { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.35); padding: 6px 12px; }
.gate { padding: 24px 0 48px; }
.gate form, .paste, .tape, .panel {
  background: #f3efe2;
  color: #1c2430;
  padding: 22px;
}
.gate form { width: min(420px, 100%); display: grid; gap: 12px; }
label { display: grid; gap: 6px; font-size: 14px; }
input, textarea {
  width: 100%;
  border: 0;
  background: #fffdf8;
  color: #1c2430;
  padding: 10px 12px;
  box-shadow: inset 0 0 0 1px #d9d0bc;
}
textarea { min-height: 220px; font-family: "IBM Plex Mono", Consolas, monospace; line-height: 1.6; }
button { background: #9d2c2c; color: #fffaf2; padding: 10px 14px; }

.tabs { display: flex; gap: 8px; padding: 8px 0 18px; }
.tab {
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.25);
  padding: 8px 18px;
}
.tab.on { border-color: #d7a15f; color: #d7a15f; }

.desk { display: grid; grid-template-columns: 360px 1fr; gap: 22px; padding-bottom: 48px; }
.paste, .slips { display: grid; gap: 12px; align-content: start; }
.rule, .quiet { margin: 0; color: #5c5648; font-size: 14px; }
.slip {
  text-align: left;
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.2);
  padding: 12px 14px;
  display: grid;
  gap: 4px;
}
.slip.on { border-color: #d7a15f; }
.slip em { font-style: normal; color: #d7a15f; }
.slip span, .tape li span { color: #5c5648; }
.tape header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.tape h4 { margin: 14px 0 8px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #e4dcc8; font-size: 14px; }
ul { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
li { display: grid; gap: 2px; }
code { font-family: Consolas, monospace; }
.warn { color: #9d2c2c; margin: 0; }

.badge {
  display: inline-block;
  padding: 3px 10px;
  font-size: 13px;
  color: #fffaf2;
  background: #45566f;
}
.badge.整批收回, .badge.rollback { background: #9d2c2c; }
.badge.回滚争用, .badge.contention { background: #8a6d1f; }
.badge.部分接纳 { background: #2f6d4f; }
tr.withdrawn td { color: #9d2c2c; text-decoration: line-through; }
mark.mismatch {
  justify-self: start;
  background: #f6d9d2;
  color: #9d2c2c;
  padding: 2px 8px;
  font-size: 13px;
}

.eventlist li {
  background: #fffdf8;
  padding: 8px 10px;
  box-shadow: inset 0 0 0 1px #e4dcc8;
}

.rollback-page, .reject-page, .policy-page {
  display: grid;
  grid-template-columns: 420px 1fr;
  gap: 22px;
  padding-bottom: 48px;
}
.policy-page, .reject-page { grid-template-columns: 1fr; }
.panel { display: grid; gap: 12px; align-content: start; }
.event-card {
  text-align: left;
  background: #fffdf8;
  color: #1c2430;
  box-shadow: inset 0 0 0 1px #d9d0bc;
  padding: 12px 14px;
  display: grid;
  gap: 4px;
}
.event-card.on { box-shadow: inset 0 0 0 2px #9d2c2c; }
.event-card span { color: #5c5648; font-size: 14px; }
.detail-head { display: flex; gap: 10px; align-items: center; }
.diff {
  display: flex;
  align-items: stretch;
  gap: 14px;
  background: #fffdf8;
  box-shadow: inset 0 0 0 1px #e4dcc8;
  padding: 14px;
}
.diff div { display: grid; gap: 4px; }
.diff strong { font-size: 26px; }
.diff .arrow { align-self: center; font-size: 22px; color: #5c5648; }
.diff .delta strong { color: #9d2c2c; }

.policy-form { display: grid; gap: 12px; }
.policy-option {
  grid-template-columns: auto 1fr;
  align-items: start;
  background: #fffdf8;
  box-shadow: inset 0 0 0 1px #d9d0bc;
  padding: 12px 14px;
  gap: 10px;
}
.policy-option input { width: auto; }
.policy-option span { display: grid; gap: 2px; }
.policy-option small { color: #5c5648; }
.locked {
  background: #e7efe8;
  box-shadow: inset 0 0 0 1px #b9d2c1;
  padding: 16px;
  display: grid;
  gap: 6px;
}
.effective { border-top: 1px solid #d9d0bc; padding-top: 12px; }

@media (max-width: 900px) {
  .desk, .rollback-page { grid-template-columns: 1fr; }
}
</style>
