function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** ISO 时间 → "MM-DD HH:mm:ss"(审计列表展示格式) */
export function fmtDateTime(iso: string | null | undefined): string {
  const d = parse(iso)
  if (!d) return '—'
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** ISO 时间 → "YYYY-MM-DD"(成员加入时间等) */
export function fmtDate(iso: string | null | undefined): string {
  const d = parse(iso)
  if (!d) return '—'
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** 距今整天数(个人中心"加入天数") */
export function daysSince(iso: string | null | undefined): number | null {
  const d = parse(iso)
  if (!d) return null
  return Math.max(0, Math.floor((Date.now() - d.getTime()) / 86_400_000))
}

/** 字节数 → 人类可读(与原型一致:8.2 MB / 356 KB) */
export function fmtBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[i]}`
}

/** ISO 时间 → "今天 10:21 / 昨天 16:40 / 09-12 14:05" */
export function fmtStamp(iso: string | null | undefined): string {
  const d = parse(iso)
  if (!d) return '—'
  const now = new Date()
  const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  if (sameDay(d, now)) return `今天 ${clock}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(d, yesterday)) return `昨天 ${clock}`
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock}`
}

/** ISO 时间 → "2 小时前 / 3 天前 / 09-02" */
export function fmtRelative(iso: string | null | undefined): string {
  const d = parse(iso)
  if (!d) return '—'
  const diff = Date.now() - d.getTime()
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days === 1) return '昨天'
  if (days < 7) return `${days} 天前`
  if (days < 14) return '上周'
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** ISO 时间 → "10:31"(会话列表) */
export function fmtClock(iso: string | null | undefined): string {
  const d = parse(iso)
  if (!d) return '—'
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 会话列表时间分组:今天 / 昨天 / 本周更早 */
export function fmtDayGroup(iso: string | null | undefined): '今天' | '昨天' | '本周更早' | '更早' {
  const d = parse(iso)
  if (!d) return '更早'
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const t = d.getTime()
  if (t >= startOfToday) return '今天'
  if (t >= startOfToday - 86_400_000) return '昨天'
  if (t >= startOfToday - 7 * 86_400_000) return '本周更早'
  return '更早'
}
