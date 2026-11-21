<template>
  <section class="page" data-module="partition_readiness">
    <header class="page-head">
      <div>
        <h2>分区齐备核对</h2>
        <p class="page-desc">
          按目标表把生产日历与上游到达明细对齐，判断应产日期是否齐备；
          停产日冲突单独复核，补偿批次以实际校验通过的分区衔接。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="open-arrival" type="button" @click="openArrival">补交当天数据</button>
        <button class="btn" data-testid="reload" type="button" @click="reloadAll">刷新口径</button>
        <button class="btn primary" data-testid="plan-batches" type="button" :disabled="planning" @click="handlePlan">
          {{ planning ? '生成中…' : '按清单生成补偿批次' }}
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="card in summaryCards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>
    <p v-if="actionMessage" class="ok-text" data-testid="message">{{ actionMessage }}</p>

    <nav class="tab-row">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        type="button"
        :data-testid="`tab-${tab.key}`"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- 待补分区清单 -->
    <section v-show="activeTab === 'pending'" class="panel">
      <form class="filter-bar" @submit.prevent="loadPendingRows">
        <label class="filter-item">
          <span>目标表</span>
          <select v-model="pendingFilter.table" data-testid="filter-table">
            <option value="">全部</option>
            <option v-for="rule in rules" :key="rule.target_table" :value="rule.target_table">
              {{ rule.target_table }}
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>状态</span>
          <select v-model="pendingFilter.status" data-testid="filter-status">
            <option value="">全部</option>
            <option v-for="item in stateOptions" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="filter-item inline">
          <input v-model="pendingFilter.conflictOnly" type="checkbox" data-testid="filter-conflict" />
          <span>只看停产冲突</span>
        </label>
        <button class="btn" data-testid="query-pending" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetPendingFilter">重置</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>目标表</th><th>业务日期</th><th>星期</th><th>应产判断</th>
            <th>应产确认日</th><th>实到日期</th><th>到达行数</th><th>齐备率</th>
            <th>校验状态</th><th>状态</th><th>处理动作</th><th>日历版本</th>
            <th>判断有效日期</th><th>下游可读</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in pendingRows"
            :key="String(row.partition_id)"
            :data-testid="`pending-${row['目标表']}-${row['业务日期']}`"
            :data-status="String(row['状态'])"
          >
            <td>{{ row['目标表'] }}</td>
            <td>{{ row['业务日期'] }}</td>
            <td>{{ row['星期'] }}</td>
            <td>{{ row['应产判断'] }}</td>
            <td>{{ row['应产确认日'] }}</td>
            <td>{{ row['实到日期'] }}</td>
            <td>{{ row['到达行数'] }}</td>
            <td>{{ row['齐备率'] }}</td>
            <td>{{ row['校验状态'] }}</td>
            <td>{{ row['状态'] }}</td>
            <td>{{ row['处理动作'] }}</td>
            <td>{{ row['日历版本'] }}</td>
            <td>{{ row['判断有效日期'] }}</td>
            <td>{{ row['下游可读'] }}</td>
            <td class="row-actions">
              <button
                v-if="row['停产冲突'] === '是'"
                class="link"
                type="button"
                :data-testid="`review-${row['目标表']}-${row['业务日期']}`"
                @click="openConflict(String(row['目标表']), String(row['业务日期']))"
              >
                复核冲突
              </button>
              <span v-else>—</span>
            </td>
          </tr>
          <tr v-if="!pendingRows.length">
            <td :colspan="15" class="empty-state">当前筛选下没有待补分区</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 补偿批次 -->
    <section v-show="activeTab === 'batches'" class="panel">
      <p class="hint">
        批次间串行衔接：下一批的输入取上一批的日期，只有这些分区实际校验通过后批次才可调度。
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次</th><th>目标表</th><th>补偿日期</th><th>输入分区</th>
            <th>输入就绪</th><th>状态</th><th>日历版本</th><th>规则版本</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="batch.batch_id" :data-testid="`batch-${batch.batch_id}`">
            <td>{{ batch.batch_id }}</td>
            <td>{{ batch.target_table }}</td>
            <td>{{ batch.dates.join('、') }}</td>
            <td>{{ batch.input_partitions.length ? batch.input_partitions.join('、') : '起始批次' }}</td>
            <td>{{ batch.input_ready ? '是' : '否' }}</td>
            <td>{{ batch.status }}</td>
            <td>{{ batch.calendar_version }}</td>
            <td>{{ batch.rule_version }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`detail-${batch.batch_id}`" @click="openBatch(batch.batch_id)">
                核对明细
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`schedule-${batch.batch_id}`"
                :disabled="batch.status === '已完成'"
                @click="handleSchedule(batch.batch_id)"
              >
                调度
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`complete-${batch.batch_id}`"
                :disabled="batch.status !== '调度中'"
                @click="handleComplete(batch.batch_id)"
              >
                完成补偿
              </button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="9" class="empty-state">还没有补偿批次，先按待补清单生成</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 规则与日历 -->
    <section v-show="activeTab === 'rules'" class="panel">
      <h3>目标表应产规则</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>目标表</th><th>管线</th><th>生产日历</th><th>日历版本</th>
            <th>确认阈值</th><th>齐备率阈值</th><th>规则版本</th><th>待补数</th><th>冲突数</th><th>可读日期</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="rule in rules" :key="rule.target_table">
            <td>{{ rule.target_table }}</td>
            <td>{{ rule.pipeline }}</td>
            <td>{{ rule.calendar_id }}</td>
            <td>{{ rule.calendar_version }}</td>
            <td>T+{{ rule.threshold_days }}</td>
            <td>{{ rule.completeness_threshold }}%</td>
            <td>{{ rule.rule_version }}</td>
            <td>{{ rule.pending_count }}</td>
            <td>{{ rule.conflict_count }}</td>
            <td>{{ rule.readable_dates.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h3>生产日历</h3>
      <table class="data-table">
        <thead>
          <tr><th>日历编码</th><th>版本</th><th>生效区间</th><th>停产日</th><th>状态</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="calendar in calendars" :key="calendar.calendar_id">
            <td>{{ calendar.calendar_id }}</td>
            <td>{{ calendar.version }}</td>
            <td>{{ calendar.effective_from }} ~ {{ calendar.effective_to }}</td>
            <td>{{ calendar.shutdown_dates.join('、') }}</td>
            <td>{{ calendar.status }}</td>
            <td>{{ calendar.note || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <form class="analysis-form" @submit.prevent="handleCalendarCorrect">
        <label>
          <span>生产日历编码</span>
          <input v-model="calendarForm.calendarId" data-testid="calendar-id" />
        </label>
        <label>
          <span>停产日（逗号分隔）</span>
          <input v-model="calendarForm.shutdownDates" data-testid="calendar-shutdown" />
        </label>
        <label>
          <span>修订说明</span>
          <input v-model="calendarForm.note" data-testid="calendar-note" />
        </label>
        <button class="btn primary" data-testid="submit-calendar" type="submit">更正生产日历</button>
      </form>

      <form class="analysis-form" @submit.prevent="handleRuleChange">
        <label>
          <span>目标表</span>
          <select v-model="ruleForm.table" data-testid="rule-table">
            <option v-for="rule in rules" :key="rule.target_table" :value="rule.target_table">
              {{ rule.target_table }}
            </option>
          </select>
        </label>
        <label>
          <span>确认阈值天数</span>
          <input v-model.number="ruleForm.thresholdDays" type="number" min="0" data-testid="rule-threshold" />
        </label>
        <label>
          <span>齐备率阈值(%)</span>
          <input v-model.number="ruleForm.completeness" type="number" min="0" max="100" step="0.01" data-testid="rule-completeness" />
        </label>
        <button class="btn primary" data-testid="submit-rule" type="submit">变更规则口径</button>
      </form>
    </section>
  </section>

  <!-- 冲突复核抽屉 -->
  <div v-if="activeConflict" class="modal-backdrop" data-testid="conflict-modal">
    <section class="modal">
      <h3>停产日冲突复核 · {{ activeConflict.target_table }}</h3>
      <p class="hint">{{ activeConflict.conflict_date }} · {{ activeConflict.calendar_version }}</p>
      <p class="hint">额外到达：{{ activeConflict.extra_arrival?.row_count ?? 0 }} 行，齐备率 {{ activeConflict.extra_arrival?.completeness ?? 0 }}%</p>
      <p data-testid="conflict-recommendation">{{ activeConflict.recommendation }}</p>
      <table class="data-table">
        <thead><tr><th>相邻日期</th><th>星期</th><th>状态</th><th>下游可读</th><th>当期影响</th></tr></thead>
        <tbody>
          <tr v-for="item in activeConflict.adjacent_dates" :key="String(item.biz_date)">
            <td>{{ item.biz_date }}</td>
            <td>{{ item.weekday }}</td>
            <td>{{ item.state }}</td>
            <td>{{ item.readable ? '是' : '否' }}</td>
            <td>{{ item.input_effect }}</td>
          </tr>
        </tbody>
      </table>
      <div class="page-actions">
        <button class="btn" type="button" @click="activeConflict = null">关闭</button>
        <button class="btn" data-testid="resolve-wait" type="button" @click="handleResolve('等待修订')">
          等待停产口径修订
        </button>
        <button class="btn primary" data-testid="resolve-include" type="button" @click="handleResolve('纳入当期')">
          确认纳入当期
        </button>
      </div>
    </section>
  </div>

  <!-- 补偿任务核对抽屉 -->
  <div v-if="activeBatch" class="modal-backdrop" data-testid="batch-modal">
    <section class="modal">
      <h3>补偿任务核对 · {{ activeBatch.batch_id }}</h3>
      <dl class="dag-detail">
        <div><dt>目标表</dt><dd>{{ activeBatch.target_table }}</dd></div>
        <div><dt>选择的日期</dt><dd data-testid="detail-dates">{{ activeBatch.selected_dates.join('、') }}</dd></div>
        <div><dt>输入分区</dt><dd>{{ activeBatch.input_partitions.join('、') || '起始批次' }}</dd></div>
        <div><dt>输入就绪</dt><dd>{{ activeBatch.input_ready ? '是' : '否' }}</dd></div>
        <div><dt>日历版本</dt><dd>{{ activeBatch.calendar_version }}</dd></div>
        <div><dt>规则版本</dt><dd>{{ activeBatch.rule_version }}</dd></div>
      </dl>
      <h4>待确认明细</h4>
      <table class="data-table">
        <thead><tr><th>业务日期</th><th>状态</th><th>处理动作</th><th>待补原因</th><th>到达证据</th><th>下游可读</th></tr></thead>
        <tbody>
          <tr v-for="row in activeBatch.pending_detail" :key="row['业务日期']">
            <td>{{ row['业务日期'] }}</td>
            <td>{{ row['状态'] }}</td>
            <td>{{ row['处理动作'] }}</td>
            <td>{{ row['待补原因'] }}</td>
            <td>{{ row['到达证据'] }}</td>
            <td>{{ row['下游可读'] }}</td>
          </tr>
        </tbody>
      </table>
      <h4>日汇总范围</h4>
      <p class="hint" data-testid="detail-summary">
        {{ activeBatch.daily_summary.scope_start }} ~ {{ activeBatch.daily_summary.scope_end }}，
        共 {{ activeBatch.daily_summary.day_count }} 天，到达 {{ activeBatch.daily_summary.arrived_days }} 天，
        校验通过 {{ activeBatch.daily_summary.validated_days }} 天，
        累计 {{ activeBatch.daily_summary.total_rows }} 行
      </p>
      <table class="data-table">
        <thead><tr><th>日期</th><th>星期</th><th>停产</th><th>到达</th><th>行数</th><th>齐备率</th><th>已校验</th></tr></thead>
        <tbody>
          <tr v-for="day in activeBatch.daily_summary.days" :key="String(day.biz_date)">
            <td>{{ day.biz_date }}</td>
            <td>{{ day.weekday }}</td>
            <td>{{ day.shutdown ? '是' : '否' }}</td>
            <td>{{ day.arrived ? '是' : '否' }}</td>
            <td>{{ day.row_count }}</td>
            <td>{{ day.completeness }}%</td>
            <td>{{ day.validated ? '是' : '否' }}</td>
          </tr>
        </tbody>
      </table>
      <div class="page-actions">
        <button class="btn" type="button" @click="activeBatch = null">关闭</button>
      </div>
    </section>
  </div>

  <!-- 补交到达明细 -->
  <div v-if="showArrival" class="modal-backdrop" data-testid="arrival-modal">
    <form class="modal" @submit.prevent="handleArrival">
      <h3>补交当天到达明细</h3>
      <label class="create-field">
        <span>目标表</span>
        <select v-model="arrivalForm.table" data-testid="arrival-table">
          <option v-for="rule in rules" :key="rule.target_table" :value="rule.target_table">
            {{ rule.target_table }}
          </option>
        </select>
      </label>
      <label class="create-field">
        <span>业务日期</span>
        <input v-model="arrivalForm.bizDate" data-testid="arrival-date" placeholder="2026-11-15" required />
      </label>
      <label class="create-field">
        <span>到达行数</span>
        <input v-model.number="arrivalForm.rowCount" type="number" min="0" data-testid="arrival-rows" required />
      </label>
      <label class="create-field">
        <span>齐备率(%)</span>
        <input v-model.number="arrivalForm.completeness" type="number" min="0" max="100" step="0.01" data-testid="arrival-completeness" required />
      </label>
      <label class="create-field">
        <span>来源批次</span>
        <input v-model="arrivalForm.sourceBatch" data-testid="arrival-batch" required />
      </label>
      <label class="create-field">
        <span>到达证据</span>
        <input v-model="arrivalForm.evidence" data-testid="arrival-evidence" placeholder="路径/_SUCCESS@时间" />
      </label>
      <div class="page-actions">
        <button class="btn" type="button" @click="showArrival = false">取消</button>
        <button class="btn primary" data-testid="submit-arrival" type="submit">提交补交</button>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  changeRule,
  completeBatch,
  correctCalendar,
  loadBatchDetail,
  loadBatches,
  loadPending,
  loadReadinessOverview,
  planBatches,
  resolveConflict,
  reviewConflict,
  scheduleBatch,
  submitArrival,
  type BatchDetail,
  type BatchRow,
  type ConflictReview,
  type PendingRow,
} from '@/api/backend-service'

type RuleRow = {
  target_table: string
  pipeline: string
  calendar_id: string
  calendar_version: string
  threshold_days: number
  completeness_threshold: number
  rule_version: string
  pending_count: number
  conflict_count: number
  readable_dates: string[]
}

type CalendarRow = {
  calendar_id: string
  version: string
  effective_from: string
  effective_to: string
  shutdown_dates: string[]
  status: string
  note: string
}

const tabs = [
  { key: 'pending', label: '待补分区清单' },
  { key: 'batches', label: '补偿批次' },
  { key: 'rules', label: '规则与日历' },
]
const activeTab = ref('pending')
const rules = ref<RuleRow[]>([])
const calendars = ref<CalendarRow[]>([])
const pendingRows = ref<PendingRow[]>([])
const batches = ref<BatchRow[]>([])
const summary = ref({ total: 0, conflicts: 0, byState: {} as Record<string, number> })
const errorMessage = ref('')
const actionMessage = ref('')
const planning = ref(false)
const activeConflict = ref<ConflictReview | null>(null)
const activeBatch = ref<BatchDetail | null>(null)
const showArrival = ref(false)

const pendingFilter = ref({ table: '', status: '', conflictOnly: false })
const stateOptions = ['待补', '待校验', '待确认', '已完成', '已补偿']
const calendarForm = ref({ calendarId: 'CAL-2026-11-R1', shutdownDates: '', note: '' })
const ruleForm = ref({ table: '', thresholdDays: 1, completeness: 99 })
const arrivalForm = ref({
  table: '',
  bizDate: '',
  rowCount: 0,
  completeness: 0,
  sourceBatch: '',
  evidence: '',
})

const summaryCards = computed(() => [
  { label: '待补清单总数', value: summary.value.total },
  { label: '停产日冲突', value: summary.value.conflicts },
  { label: '待补', value: summary.value.byState['待补'] ?? 0 },
  { label: '待校验', value: summary.value.byState['待校验'] ?? 0 },
  { label: '补偿批次', value: batches.value.length },
])

function setError(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
}

async function loadReadinessMeta() {
  const payload = (await loadReadinessOverview()) as {
    rules?: RuleRow[]
    calendars?: CalendarRow[]
  }
  rules.value = payload.rules ?? []
  calendars.value = payload.calendars ?? []
  if (rules.value.length && !ruleForm.value.table) {
    ruleForm.value.table = rules.value[0].target_table
    ruleForm.value.thresholdDays = rules.value[0].threshold_days
    ruleForm.value.completeness = rules.value[0].completeness_threshold
  }
  if (rules.value.length && !arrivalForm.value.table) {
    arrivalForm.value.table = rules.value[0].target_table
  }
}

async function loadPendingRows() {
  errorMessage.value = ''
  try {
    const payload = await loadPending({
      table: pendingFilter.value.table || undefined,
      status: pendingFilter.value.status || undefined,
      conflictOnly: pendingFilter.value.conflictOnly,
    })
    pendingRows.value = payload.items
    summary.value = {
      total: payload.summary.total,
      conflicts: payload.summary.conflicts,
      byState: payload.summary.by_state,
    }
  } catch (error) {
    setError(error, '待补分区清单读取失败')
  }
}

async function loadBatchRows() {
  try {
    const payload = await loadBatches()
    batches.value = payload.items
  } catch (error) {
    setError(error, '补偿批次读取失败')
  }
}

async function reloadAll() {
  actionMessage.value = ''
  try {
    await loadReadinessMeta()
    await loadPendingRows()
    await loadBatchRows()
  } catch (error) {
    setError(error, '刷新失败')
  }
}

function resetPendingFilter() {
  pendingFilter.value = { table: '', status: '', conflictOnly: false }
  void loadPendingRows()
}

async function handlePlan() {
  planning.value = true
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await planBatches([])
    const created = (result.created_batches as BatchRow[]) ?? []
    const excluded = (result.excluded as Record<string, string>[]) ?? []
    actionMessage.value = `生成 ${created.length} 个补偿批次；排除 ${excluded.length} 个待确认日期`
    await loadBatchRows()
    await loadPendingRows()
    activeTab.value = 'batches'
  } catch (error) {
    setError(error, '生成补偿批次失败')
  } finally {
    planning.value = false
  }
}

async function handleSchedule(batchId: string) {
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await scheduleBatch(batchId)
    actionMessage.value = `批次 ${batchId} 已调度，日期：${(result.selected_dates as string[]).join('、')}`
    await loadBatchRows()
  } catch (error) {
    setError(error, '调度失败')
  }
}

async function handleComplete(batchId: string) {
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    await completeBatch(batchId)
    actionMessage.value = `批次 ${batchId} 已完成补偿，日期保留在原批次`
    await loadBatchRows()
    await loadPendingRows()
  } catch (error) {
    setError(error, '完成补偿失败')
  }
}

async function openConflict(table: string, bizDate: string) {
  errorMessage.value = ''
  try {
    activeConflict.value = await reviewConflict(table, bizDate)
  } catch (error) {
    setError(error, '停产冲突复核失败')
  }
}

async function handleResolve(resolution: string) {
  if (!activeConflict.value) return
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await resolveConflict(
      String(activeConflict.value.target_table),
      activeConflict.value.conflict_date,
      resolution,
    )
    actionMessage.value = `已记录结论：${resolution}；${String(result.recommendation ?? '')}`
    activeConflict.value = null
    await loadPendingRows()
    await loadBatchRows()
  } catch (error) {
    setError(error, '记录停产冲突结论失败')
  }
}

function openArrival() {
  arrivalForm.value = {
    table: rules.value[0]?.target_table ?? '',
    bizDate: '',
    rowCount: 0,
    completeness: 0,
    sourceBatch: '',
    evidence: '',
  }
  errorMessage.value = ''
  showArrival.value = true
}

async function openBatch(batchId: string) {  errorMessage.value = ''
  try {
    activeBatch.value = await loadBatchDetail(batchId)
  } catch (error) {
    setError(error, '补偿任务核对失败')
  }
}

async function handleCalendarCorrect() {
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const shutdownDates = calendarForm.value.shutdownDates
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
    const result = await correctCalendar({
      calendar_id: calendarForm.value.calendarId,
      shutdown_dates: shutdownDates,
      note: calendarForm.value.note || '生产日历更正',
    })
    actionMessage.value = `生产日历已更正为 ${String(result.version)}，影响 ${(result.affected_tables as string[]).length} 张表`
    await reloadAll()
  } catch (error) {
    setError(error, '更正生产日历失败')
  }
}

async function handleRuleChange() {
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await changeRule({
      table: ruleForm.value.table,
      threshold_days: ruleForm.value.thresholdDays,
      completeness_threshold: ruleForm.value.completeness,
      reason: '阈值口径变更（页面操作）',
    })
    actionMessage.value = `规则版本 ${String(result.old_rule_version)} → ${String(result.new_rule_version)}，重算 ${(result.reclassified as unknown[]).length} 个日期`
    await reloadAll()
  } catch (error) {
    setError(error, '变更规则口径失败')
  }
}

async function handleArrival() {
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const result = await submitArrival({
      table: arrivalForm.value.table,
      biz_date: arrivalForm.value.bizDate,
      arrived_at: new Date().toISOString(),
      row_count: arrivalForm.value.rowCount,
      completeness: arrivalForm.value.completeness,
      source_batch: arrivalForm.value.sourceBatch,
      evidence: arrivalForm.value.evidence,
    })
    const assessment = result.assessment as Record<string, unknown> | null
    const affected = (result.affected_batches as Array<{ batch_id: string }>) ?? []
    const batchNote = affected.length
      ? `；关联批次 ${affected.map((item) => item.batch_id).join('、')}`
      : ''
    actionMessage.value = `补交已登记，判定状态：${String(assessment?.state ?? '—')}${batchNote}`
    showArrival.value = false
    await loadPendingRows()
    await loadBatchRows()
    if (assessment?.conflict) {
      activeConflict.value = result.conflict as ConflictReview
    }
  } catch (error) {
    setError(error, '补交到达明细失败')
  }
}

onMounted(reloadAll)
</script>
