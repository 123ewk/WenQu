/** 极简 Markdown 渲染(不引入富文本依赖)。
 *  输入先整体转义,再按块/行内规则还原有限语法,避免 XSS;
 *  答案里的 [n] 渲染为可点击引用角标(点击由容器上的事件委托处理)。
 */

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function renderInline(text: string, citeCount: number): string {
  const codes: string[] = []
  // 行内代码先抽走,避免其中的 [n] / ** 被后续规则误伤
  let out = text.replace(/`([^`]+)`/g, (_m, code: string) => {
    codes.push(code)
    return `\u0000${codes.length - 1}\u0000`
  })
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  if (citeCount > 0) {
    out = out.replace(/\[(\d+)\]/g, (match, digits: string) => {
      const n = Number(digits)
      if (n < 1 || n > citeCount) return match
      return `<button type="button" class="cite" data-cite="${n}" title="查看引用 ${n}">${n}</button>`
    })
  }
  out = out.replace(/\u0000(\d+)\u0000/g, (_m, i: string) => `<span class="icode">${codes[Number(i)]}</span>`)
  return out
}

export function renderMarkdown(source: string, citeCount = 0): string {
  const lines = escapeHtml(source).split('\n')
  const out: string[] = []
  let paragraph: string[] = []
  let list: { ordered: boolean; items: string[] } | null = null
  let fence: { lang: string; body: string[] } | null = null

  const flushParagraph = () => {
    if (!paragraph.length) return
    out.push(`<p>${renderInline(paragraph.join('<br>'), citeCount)}</p>`)
    paragraph = []
  }
  const flushList = () => {
    if (!list) return
    const tag = list.ordered ? 'ol' : 'ul'
    const items = list.items.map((item) => `<li>${renderInline(item, citeCount)}</li>`).join('')
    out.push(`<${tag}>${items}</${tag}>`)
    list = null
  }
  const flushFence = () => {
    if (!fence) return
    const lang = fence.lang || 'text'
    out.push(
      `<div class="codeblock"><div class="codeblock-head"><span class="cb-lang">${lang}</span>` +
        `<button type="button" class="cb-copy" data-copy>复制</button></div>` +
        `<pre><code>${fence.body.join('\n')}</code></pre></div>`,
    )
    fence = null
  }

  for (const line of lines) {
    const trimmed = line.trim()

    if (fence) {
      if (trimmed.startsWith('```')) flushFence()
      else fence.body.push(line)
      continue
    }
    if (trimmed.startsWith('```')) {
      flushParagraph()
      flushList()
      fence = { lang: trimmed.slice(3).trim(), body: [] }
      continue
    }
    if (!trimmed) {
      flushParagraph()
      flushList()
      continue
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed)
    if (heading) {
      flushParagraph()
      flushList()
      out.push(`<h3>${renderInline(heading[2], citeCount)}</h3>`)
      continue
    }
    const ordered = /^(\d+)\.\s+(.*)$/.exec(trimmed)
    if (ordered) {
      flushParagraph()
      if (!list?.ordered) {
        flushList()
        list = { ordered: true, items: [] }
      }
      list.items.push(ordered[2])
      continue
    }
    const bullet = /^[-*+]\s+(.*)$/.exec(trimmed)
    if (bullet) {
      flushParagraph()
      if (!list || list.ordered) {
        flushList()
        list = { ordered: false, items: [] }
      }
      list.items.push(bullet[1])
      continue
    }
    flushList()
    paragraph.push(trimmed)
  }
  flushParagraph()
  flushList()
  flushFence()
  return out.join('')
}
