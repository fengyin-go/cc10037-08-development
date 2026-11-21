import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 分区齐备核对 ----

export type PendingRow = Record<string, string | number | boolean>

export type PendingResponse = {
  items: PendingRow[]
  total: number
  summary: {
    total: number
    by_state: Record<string, number>
    conflicts: number
    tables: string[]
    window: { start: string; end: string }
    last_rebuild: {
      judgment_id?: string
      reason?: string
      at?: string
      changed?: Record<string, string>[]
    }
  }
}

export type ConflictReview = {
  conflict_date: string
  has_conflict: boolean
  message?: string
  target_table?: string
  pipeline?: string
  calendar_version?: string
  resolution?: string
  includable?: boolean
  extra_arrival?: Record<string, string | number> | null
  adjacent_dates?: Array<Record<string, string | number | boolean>>
  recommendation?: string
}

export type BatchRow = {
  batch_id: string
  target_table: string
  pipeline: string
  dates: string[]
  input_partitions: string[]
  input_ready: boolean
  status: string
  calendar_version: string
  rule_version: string
  missing_inputs?: string[]
  validated_inputs?: string[]
  arrival_evidence?: Record<string, string>
  [key: string]: unknown
}

export type BatchDetail = {
  batch_id: string
  status: string
  target_table: string
  pipeline: string
  selected_dates: string[]
  input_partitions: string[]
  input_ready: boolean
  calendar_version: string
  rule_version: string
  pending_detail: Record<string, string>[]
  confirmation_detail: Record<string, string | number | boolean>[]
  daily_summary: {
    scope_start: string
    scope_end: string
    day_count: number
    total_rows: number
    arrived_days: number
    validated_days: number
    days: Array<Record<string, string | number | boolean>>
  }
}

async function postJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) {
    const detail = (payload as { detail?: string }).detail
    throw new Error(detail ?? `接口返回 ${response.status}`)
  }
  return payload as T
}

export async function loadReadinessOverview(): Promise<Record<string, unknown>> {
  const response = await request('/api/readiness/overview')
  if (!response.ok) throw new Error('分区齐备概览读取失败')
  return await response.json()
}

export async function loadPending(
  params: { table?: string; status?: string; conflictOnly?: boolean } = {},
): Promise<PendingResponse> {
  const query = new URLSearchParams()
  if (params.table) query.set('table', params.table)
  if (params.status) query.set('status', params.status)
  if (params.conflictOnly) query.set('conflict_only', 'true')
  const response = await request(`/api/readiness/pending?${query.toString()}`)
  if (!response.ok) throw new Error('待补分区清单读取失败')
  return await response.json()
}

export async function reviewConflict(table: string, bizDate: string): Promise<ConflictReview> {
  const query = new URLSearchParams({ table, biz_date: bizDate })
  const response = await request(`/api/readiness/conflict?${query.toString()}`)
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '停产冲突核对失败')
  return payload
}

export async function resolveConflict(
  table: string,
  bizDate: string,
  resolution: string,
): Promise<Record<string, unknown>> {
  return postJson('/api/readiness/conflict/resolve', {
    table,
    biz_date: bizDate,
    resolution,
  })
}

export async function planBatches(
  tables: string[],
  maxPerBatch = 3,
): Promise<Record<string, unknown>> {
  return postJson('/api/readiness/batches/plan', {
    tables,
    max_per_batch: maxPerBatch,
    reason: '按待补分区清单生成补偿批次',
  })
}

export async function loadBatches(): Promise<{ items: BatchRow[]; total: number }> {
  const response = await request('/api/readiness/batches')
  if (!response.ok) throw new Error('补偿批次读取失败')
  return await response.json()
}

export async function scheduleBatch(batchId: string): Promise<Record<string, unknown>> {
  return postJson(`/api/readiness/batches/${batchId}/schedule`)
}

export async function completeBatch(batchId: string): Promise<Record<string, unknown>> {
  return postJson(`/api/readiness/batches/${batchId}/complete`)
}

export async function loadBatchDetail(batchId: string): Promise<BatchDetail> {
  const response = await request(`/api/readiness/batches/${batchId}`)
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '补偿任务核对失败')
  return payload
}

export async function makeReadable(table?: string): Promise<{ items: Record<string, unknown>[]; total: number }> {
  const query = table ? `?table=${encodeURIComponent(table)}` : ''
  const response = await request(`/api/readiness/readable${query}`)
  if (!response.ok) throw new Error('可读分区读取失败')
  return await response.json()
}

export async function recordValidation(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  return postJson('/api/readiness/validations', payload)
}

export async function submitArrival(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  return postJson('/api/readiness/arrivals', payload)
}

export async function correctCalendar(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  return postJson('/api/readiness/calendar/correct', payload)
}

export async function changeRule(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  return postJson('/api/readiness/rules/change', payload)
}
