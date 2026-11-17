<template>
  <section class="page" data-module="proxy">
    <header class="page-head">
      <div>
        <h2>采集代理路由管理</h2>
        <p class="page-desc">
          维护采集代理的地址配置、区域标签与可用性监控；代理容量按
          floor(并发上限 × 可用比例 × 延迟系数) 计算，和频率规则、机器人下发共用同一口径。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记代理配置</button>
        <button class="btn" type="button" @click="exportRows">导出采集代理路由清单</button>
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
          <h3>代理容量清单</h3>
          <p class="panel-desc">
            迁移版本 {{ capacity.迁移版本 || '—' }} · 容量口径 {{ capacity.容量口径 || '—' }}
          </p>
        </div>
        <div class="panel-actions">
          <button class="btn" type="button" @click="reloadAll">刷新</button>
        </div>
      </header>
      <p v-if="notice" class="notice-text">{{ notice }}</p>
      <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

      <h4 class="section-title">规则复核</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>站点</th>
            <th>状态</th>
            <th>规则版本</th>
            <th>结构版本</th>
            <th>结果版本</th>
            <th>并发上限</th>
            <th>代理容量</th>
            <th>失败重试数</th>
            <th>待执行计划数量</th>
            <th>可复核字段</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in reviewRows" :key="String(row.站点)">
            <td>{{ row.目标站点 }}</td>
            <td>{{ row.状态 }}</td>
            <td>{{ row.规则版本 }}</td>
            <td>{{ row.结构版本 }}</td>
            <td>{{ row.结果版本 }}</td>
            <td>{{ row.生效并发上限 }}（旧 {{ row.旧并发上限 }} / 新 {{ row.新并发上限 }}）</td>
            <td>{{ row.代理容量 }}</td>
            <td>{{ row.失败重试数 }}</td>
            <td>{{ row.待执行计划数量 }}</td>
            <td>{{ (row.可复核字段 || []).join('、') }}</td>
          </tr>
          <tr v-if="!reviewRows.length">
            <td colspan="10" class="empty-state">还没有频率结果，可先到采集频率控制执行迁移</td>
          </tr>
        </tbody>
      </table>

      <h4 class="section-title">代理配置容量</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>配置编号</th>
            <th>区域标签</th>
            <th>可用比例</th>
            <th>响应延迟</th>
            <th>并发上限</th>
            <th>可用容量</th>
            <th>配置状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in proxyRows" :key="String(row.配置编号)">
            <td>{{ row.配置编号 }}</td>
            <td>{{ row.区域标签 }}</td>
            <td>{{ row.可用比例 }}</td>
            <td>{{ row.响应延迟 }}ms</td>
            <td>{{ row.并发上限 }}</td>
            <td>{{ row.可用容量 }}</td>
            <td>{{ row.配置状态 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <header class="panel-head">
        <div>
          <h3>代理路由输出</h3>
          <p class="panel-desc">按采集频率结果输出可用容量，机器人执行从这里读取。</p>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>站点</th>
            <th>区域</th>
            <th>可用容量</th>
            <th>生效并发上限</th>
            <th>请求间隔</th>
            <th>日配额上限</th>
            <th>结果版本</th>
            <th>待执行计划数量</th>
            <th>可执行</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in dispatchRows" :key="String(row.站点)">
            <td>{{ row.站点 }}</td>
            <td>{{ row.区域标签 }}</td>
            <td>{{ row.可用容量 }}</td>
            <td>{{ row.并发上限 }}</td>
            <td>{{ row.请求间隔 }}s</td>
            <td>{{ row.日配额上限 }}</td>
            <td>{{ row.结果版本 }}</td>
            <td>{{ row.待执行计划数量 }}</td>
            <td>{{ row.可执行 ? '是' : '待复核' }}</td>
          </tr>
          <tr v-if="!dispatchRows.length">
            <td colspan="9" class="empty-state">暂无代理路由输出，可先执行迁移</td>
          </tr>
        </tbody>
      </table>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集代理路由数据，可先登记代理配置</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集代理路由记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>
type AnyRow = Record<string, any>

const ENDPOINT = '/api/proxy'
const columns = ["配置编号", "代理地址", "代理协议", "区域标签", "可用比例", "切换阈值", "响应延迟", "并发上限", "配置状态"]
const actions = ["启用代理", "降级代理", "关闭代理"]
const stats = ref([
  { label: '代理配置总数', value: 0 },
  { label: '已启用代理', value: 0 },
  { label: '可用容量合计', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const notice = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const capacity = ref<AnyRow>({})
const reviewRows = computed<AnyRow[]>(() => capacity.value.规则复核 ?? [])
const proxyRows = computed<AnyRow[]>(() => capacity.value.代理配置 ?? [])
const dispatchRows = ref<AnyRow[]>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '代理配置登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集代理路由动作未生效，请稍后重试')
    }
    const payload = await response.json()
    notice.value = payload.message ?? ''
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集代理路由操作失败'
  }
}

async function reloadAll() {
  await Promise.all([reload(), loadCapacity(), loadDispatch()])
}

async function loadCapacity() {
  try {
    capacity.value = await fetchJson<AnyRow>(`${ENDPOINT}/capacity/review`)
  } catch {
    capacity.value = {}
  }
}

async function loadDispatch() {
  try {
    const payload = await fetchJson<AnyRow>(`${ENDPOINT}/capacity/robot-dispatch`)
    dispatchRows.value = payload.items ?? []
  } catch {
    dispatchRows.value = []
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('代理配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '代理配置总数', value: payload.total ?? rows.value.length },
      { label: '已启用代理', value: rows.value.filter((row) => row.配置状态 === '已启用').length },
      {
        label: '可用容量合计',
        value: proxyRows.value.reduce((sum: number, row: AnyRow) => sum + Number(row.可用容量 ?? 0), 0),
      },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集代理路由列表读取失败'
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
.section-title { font-size: 13px; margin: 12px 0 6px; color: var(--muted); }
.notice-text { color: #1f6feb; font-size: 12px; margin: 4px 0; }
</style>
