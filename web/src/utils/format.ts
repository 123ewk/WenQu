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
