<template>
  <section class="page" data-module="retention">
    <header class="page-head">
      <div>
        <h2>数据保留策略管理</h2>
        <p class="page-desc">维护数据保留的期限设定、过期动作与合规依据，为数据保留策略管理提供可审计的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记保留规则</button>
        <button class="btn" type="button" @click="exportRows">导出数据保留策略清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div v-if="migration.available" class="migration-banner" data-testid="migration-banner">
      <span>强类型迁移：已规范化 <strong>{{ migration.migrated }}</strong> 条 · 待处理 <strong>{{ migration.pending }}</strong> 条 · 批次 {{ migration.batches }}</span>
      <button class="link" type="button" @click="loadPending">查看待处理结果</button>
    </div>

    <div v-if="pendingRows.length" class="pending-panel" data-testid="pending-panel">
      <h3>待处理规则（原值已保留，需人工复核，未静默丢弃）</h3>
      <ul>
        <li v-for="item in pendingRows" :key="String(item['规则编号'])">
          <strong>{{ item['规则编号'] }}</strong>
          <span v-for="reason in item['待处理原因']" :key="reason" class="pending-reason">{{ reason }}</span>
        </li>
      </ul>
    </div>

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
          <td :colspan="columns.length + 1" class="empty-state">暂无数据保留策略数据，可先登记保留规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据保留策略记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/retention'
// 强类型列优先，旧口径列（保留期限/过期动作）仍保留，兼容迁移期查看与导出。
const columns = ["规则编号", "数据集范围", "规范保留天数", "规范期限", "规范过期动作", "例外清单", "合规依据", "依据版本", "执行周期", "迁移状态", "规则状态"]
const actions = ["启用规则", "废止规则", "归档规则"]
const statuses = ["草稿", "生效中", "已到期", "已废止"]
const stats = [{"label": "保留规则总数", "value": 0}, {"label": "生效中规则", "value": 0}, {"label": "已到期规则", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ["规则编号", "数据集范围", "迁移状态"]
const migration = ref<Record<string, boolean | number | string | null>>({ available: false, migrated: 0, pending: 0, batches: 0 })
const pendingRows = ref<Array<Record<string, unknown>>>([])

async function loadMigration() {
  try {
    const response = await request(`${ENDPOINT}/migration/status`)
    if (response.ok) {
      migration.value = await response.json()
    }
  } catch {
    // 存储不可用时后端回退内存仓库，迁移面板不展示即可，不影响列表。
  }
}

async function loadPending() {
  try {
    const response = await request(`${ENDPOINT}/migration/pending`)
    if (response.ok) {
      const payload = await response.json()
      pendingRows.value = payload.items ?? []
    }
  } catch {
    errorMessage.value = '待处理结果读取失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '保留规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据保留策略动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据保留策略操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('保留规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据保留策略列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadMigration()
})
</script>

<style scoped>
.migration-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  margin-bottom: 12px;
  border: 1px solid var(--border-color, #d9d9d9);
  border-radius: 8px;
  background: #f0f7ff;
  font-size: 13px;
}

.pending-panel {
  padding: 12px 14px;
  margin-bottom: 12px;
  border: 1px solid #f0c36d;
  border-radius: 8px;
  background: #fff8ec;
}

.pending-panel h3 {
  margin: 0 0 8px;
  font-size: 14px;
}

.pending-panel li {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 13px;
}

.pending-reason {
  color: #b26a00;
}
</style>
