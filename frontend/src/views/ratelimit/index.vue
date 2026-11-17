<template>
  <section class="page" data-module="ratelimit">
    <header class="page-head">
      <div>
        <h2>采集频率控制管理</h2>
        <p class="page-desc">
          维护采集频率的请求间隔、并发限制与日配额控制；迁移后按代理可用比例与响应延迟重算并发，
          站点间隔和日配额仍是硬边界。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记频率规则</button>
        <button class="btn" type="button" @click="exportRows">导出采集频率控制清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="panel">
      <header class="panel-head">
        <div>
          <h3>迁移与重算</h3>
          <p class="panel-desc">
            迁移版本 {{ migration.迁移版本 || '—' }} · 结构版本 {{ migration.结构版本 ?? '—' }} ·
            容量口径 {{ migration.容量口径 || '—' }}
          </p>
        </div>
        <div class="panel-actions">
          <button class="btn" type="button" :disabled="busy" @click="preview">迁移预览</button>
          <button class="btn primary" type="button" :disabled="busy" @click="runMigration('resume')">
            断点续跑迁移
          </button>
          <button class="btn" type="button" :disabled="busy" @click="runMigration('restart')">
            重启后继续
          </button>
          <button class="btn" type="button" :disabled="busy" @click="runMigration('redo')">全部重做</button>
        </div>
      </header>
      <div class="metric-row">
        <span>规则总数 <strong>{{ migration.规则总数 ?? 0 }}</strong></span>
        <span>已迁移 <strong>{{ migration.已迁移规则 ?? 0 }}</strong></span>
        <span>迁移失败 <strong>{{ migration.迁移失败规则 ?? 0 }}</strong></span>
        <span>当前结果 <strong>{{ migration.当前结果数 ?? 0 }}</strong></span>
        <span>待决策冲突 <strong>{{ migration.待决策冲突 ?? 0 }}</strong></span>
        <span>待执行计划 <strong>{{ migration.待执行计划 ?? 0 }}</strong></span>
      </div>
      <p v-if="migration.最近运行" class="panel-desc">
        最近运行 {{ migration.最近运行.运行编号 }} · {{ migration.最近运行.模式 }} ·
        {{ migration.最近运行.状态 }} · 处理 {{ migration.最近运行.处理规则数 }} 条 ·
        失败 {{ migration.最近运行.失败规则数 }} 条 · 回填历史 {{ migration.最近运行.回填历史数 }} 条
      </p>
      <p v-if="notice" class="notice-text">{{ notice }}</p>
      <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
      <table v-if="previewRows.length" class="data-table">
        <thead>
          <tr>
            <th>规则编号</th>
            <th>站点</th>
            <th>区域</th>
            <th>旧并发上限</th>
            <th>新并发上限</th>
            <th>生效并发上限</th>
            <th>冲突</th>
            <th>失败原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in previewRows" :key="String(row.规则编号)">
            <td>{{ row.规则编号 ?? '—' }}</td>
            <td>{{ row.站点 ?? '—' }}</td>
            <td>{{ row.区域标签 ?? '—' }}</td>
            <td>{{ row.旧并发上限 ?? '—' }}</td>
            <td>{{ row.新并发上限 ?? '—' }}</td>
            <td>{{ row.生效并发上限 ?? '—' }}</td>
            <td>{{ row.冲突 === true ? '是' : row.冲突 === false ? '否' : '—' }}</td>
            <td>{{ row.失败原因 || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <header class="panel-head">
        <div>
          <h3>代理容量复核清单</h3>
          <p class="panel-desc">每个站点只保留最后确认的一份频率结果；处理后字段可逐条复核。</p>
        </div>
        <div class="panel-actions">
          <button class="btn" type="button" @click="reloadAll">刷新</button>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>站点</th>
            <th>状态</th>
            <th>结果版本</th>
            <th>规则版本</th>
            <th>请求间隔</th>
            <th>日配额上限</th>
            <th>代理容量</th>
            <th>旧并发上限</th>
            <th>新并发上限</th>
            <th>生效并发上限</th>
            <th>失败重试数</th>
            <th>待执行计划</th>
            <th>处理方案</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in reviewRows" :key="String(row.站点)">
            <td>{{ row.目标站点 }}</td>
            <td>{{ row.状态 }}</td>
            <td>{{ row.结果版本 }}</td>
            <td>{{ row.规则版本 }}</td>
            <td>{{ row.请求间隔 }}s</td>
            <td>{{ row.日配额上限 }}</td>
            <td>{{ row.代理容量 }}</td>
            <td>{{ row.旧并发上限 }}</td>
            <td>{{ row.新并发上限 }}</td>
            <td>{{ row.生效并发上限 }}</td>
            <td>{{ row.失败重试数 }}</td>
            <td>{{ row.待执行计划数量 }}</td>
            <td>{{ row.处理方案 || '—' }}</td>
          </tr>
          <tr v-if="!reviewRows.length">
            <td colspan="13" class="empty-state">还没有当前版本的采集频率结果，可先执行迁移</td>
          </tr>
        </tbody>
      </table>

      <div v-for="row in conflictRows" :key="`conflict-${row.规则ID}`" class="conflict-card">
        <header class="conflict-head">
          <strong>{{ row.目标站点 }} 并发上限冲突</strong>
          <span class="panel-desc">
            延迟突增：响应延迟 {{ row.响应延迟 }}ms，基线 {{ row.响应延迟基线 }}ms；
            旧上限 {{ row.旧并发上限 }}，新容量上限 {{ row.新并发上限 }}
          </span>
        </header>
        <div class="conflict-actions">
          <button class="btn" type="button" :disabled="busy" @click="decide(row, 'keep_old')">
            保留旧并发上限
          </button>
          <button class="btn primary" type="button" :disabled="busy" @click="decide(row, 'adopt_new')">
            立即采用新代理容量
          </button>
          <button class="btn ghost" type="button" @click="loadTrace(row.规则ID)">查看处理明细</button>
        </div>
        <ul class="option-list">
          <li v-for="option in row.处理方案选项" :key="option.代码">
            {{ option.方案 }}：生效并发上限 {{ option.生效并发上限 }}，
            失败重试数 {{ option.失败重试数 }}，待执行计划 {{ option.待执行计划数量 }}
          </li>
        </ul>
      </div>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :disabled="busy" @click="recalculate(row)">重算频率</button>
            <button class="link" type="button" @click="loadTrace(row.id)">处理明细</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集频率控制数据，可先登记频率规则</td>
        </tr>
      </tbody>
    </table>

    <section v-if="trace" class="panel">
      <header class="panel-head">
        <div>
          <h3>处理明细 · {{ trace.规则编号 }}</h3>
          <p class="panel-desc">
            站点 {{ trace.站点 }} · 迁移状态 {{ trace.迁移状态 }} · 规则版本 {{ trace.规则版本 ?? '—' }}
          </p>
        </div>
        <div class="panel-actions">
          <button class="btn ghost" type="button" @click="trace = null">关闭</button>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>字段</th><th>原值</th><th>新值</th><th>口径</th></tr>
        </thead>
        <tbody>
          <tr v-for="(detail, field) in trace.处理明细" :key="field">
            <td>{{ field }}</td>
            <td>{{ detail.原值 ?? '—' }}</td>
            <td>{{ detail.新值 ?? '—' }}</td>
            <td>{{ detail.口径 || '—' }}</td>
          </tr>
          <tr v-if="!Object.keys(trace.处理明细 || {}).length">
            <td colspan="4" class="empty-state">该规则还没有迁移处理记录</td>
          </tr>
        </tbody>
      </table>
      <table class="data-table">
        <thead>
          <tr><th>记录时间</th><th>类型</th><th>结果版本</th><th>生效并发上限</th><th>操作人</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in trace.历史记录" :key="String(item.id)">
            <td>{{ item.记录时间 }}</td>
            <td>{{ item.类型 }}</td>
            <td>{{ item.结果版本 ?? '—' }}</td>
            <td>{{ item.生效并发上限 ?? '—' }}</td>
            <td>{{ item.操作人 ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集频率控制记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>
type ReviewRow = Record<string, any>

const ENDPOINT = '/api/ratelimit'
const MIGRATION = '/api/migration'
const columns = [
  '规则编号',
  '目标站点',
  '请求间隔',
  '并发上限',
  '日配额上限',
  '优先级调整',
  '生效时段',
  '规则版本',
  '代理容量',
  '迁移状态',
  '规则状态',
]
const actions = ['启用规则', '暂停规则', '关闭规则']
const stats = ref([
  { label: '频率规则总数', value: 0 },
  { label: '已生效规则', value: 0 },
  { label: '已迁移规则', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const notice = ref('')
const busy = ref(false)
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const migration = ref<Record<string, any>>({})
const reviewRows = ref<ReviewRow[]>([])
const previewRows = ref<ReviewRow[]>([])
const trace = ref<Record<string, any> | null>(null)
const pendingSubmissions = ref<Record<string, string>>({})

const conflictRows = computed(() =>
  reviewRows.value.filter((row) => row.冲突 && !row.处理方案代码),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '频率规则登记入口尚未接入审批流'
}

async function postJson(path: string, body: Record<string, unknown>) {
  return request(path, { method: 'POST', body: JSON.stringify(body) })
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await postJson(`${ENDPOINT}/${row.id}/actions`, { action })
    if (!response.ok) {
      throw new Error('采集频率控制动作未生效，请稍后重试')
    }
    const payload = await response.json()
    notice.value = payload.message ?? ''
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集频率控制操作失败'
  }
}

async function recalculate(row: Row) {
  if (busy.value) return
  errorMessage.value = ''
  // 同一次重算在失败重试时沿用同一个 submission_id：后端只会发布一份结果；
  // 成功后清掉，下一次点击才算新的提交。
  const key = String(row.id)
  const submissionId = pendingSubmissions.value[key] ?? `${ENDPOINT}-${row.id}-${Date.now()}`
  pendingSubmissions.value[key] = submissionId
  busy.value = true
  try {
    const response = await postJson(`${ENDPOINT}/${row.id}/migration/recalculate`, {
      submission_id: submissionId,
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '重算未生效')
    }
    delete pendingSubmissions.value[key]
    notice.value = payload.message ?? ''
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算失败，已保留原频率结果'
  } finally {
    busy.value = false
  }
}

async function preview() {
  errorMessage.value = ''
  try {
    const payload = await fetchJson<Record<string, any>>(`${MIGRATION}/preview?mode=resume`)
    previewRows.value = payload.预览 ?? []
    notice.value = `预览完成：待处理 ${payload.待处理规则数 ?? 0} 条规则`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移预览失败'
  }
}

async function runMigration(mode: 'resume' | 'restart' | 'redo') {
  busy.value = true
  errorMessage.value = ''
  try {
    const response = await postJson(`${MIGRATION}/run`, { mode })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '迁移未执行')
    }
    const run = payload.entry ?? {}
    notice.value = [
      `迁移${run.模式}：${run.状态}`,
      `处理 ${run.处理规则数 ?? 0} 条`,
      `失败 ${run.失败规则数 ?? 0} 条`,
      `回填历史 ${run.回填历史数 ?? 0} 条`,
    ].join('，')
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移执行失败'
  } finally {
    busy.value = false
  }
}

async function decide(row: ReviewRow, decision: 'keep_old' | 'adopt_new') {
  busy.value = true
  errorMessage.value = ''
  try {
    const response = await postJson(`${MIGRATION}/conflicts/${row.规则ID}/decide`, { decision })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '冲突处理未生效')
    }
    notice.value = payload.message ?? ''
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '冲突处理失败'
  } finally {
    busy.value = false
  }
}

async function loadTrace(ruleId: string | number | null) {
  if (ruleId === null) return
  errorMessage.value = ''
  try {
    trace.value = await fetchJson<Record<string, any>>(`${ENDPOINT}/${ruleId}/migration`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '处理明细读取失败'
  }
}

async function reloadAll() {
  await Promise.all([reload(), loadMigration(), loadReview()])
}

async function loadMigration() {
  try {
    migration.value = await fetchJson<Record<string, any>>(`${MIGRATION}/status`)
  } catch {
    migration.value = {}
  }
}

async function loadReview() {
  try {
    const payload = await fetchJson<Record<string, any>>(`${MIGRATION}/capacity-review`)
    reviewRows.value = payload.items ?? []
  } catch {
    reviewRows.value = []
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('频率规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '频率规则总数', value: payload.total ?? rows.value.length },
      { label: '已生效规则', value: rows.value.filter((row) => row.规则状态 === '已生效').length },
      { label: '已迁移规则', value: rows.value.filter((row) => row.迁移状态 === '已迁移').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集频率控制列表读取失败'
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 8px;
}
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-desc { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.panel-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.metric-row { display: flex; flex-wrap: wrap; gap: 16px; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.metric-row strong { color: #1f2937; }
.notice-text { color: #1f6feb; font-size: 12px; margin: 4px 0; }
.conflict-card {
  border: 1px solid #f0b429;
  background: #fffbeb;
  border-radius: 8px;
  padding: 10px 12px;
  margin-top: 10px;
}
.conflict-head { display: flex; flex-direction: column; gap: 4px; }
.conflict-actions { display: flex; gap: 8px; margin: 8px 0; }
.option-list { margin: 0; padding-left: 18px; font-size: 12px; color: var(--muted); }
</style>
