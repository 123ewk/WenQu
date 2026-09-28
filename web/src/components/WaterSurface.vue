<script setup lang="ts">
/**
 * 「一鉴开」水面背景(canvas 高度场涟漪模拟,Hugo Elias 算法):
 * 光标划过/点击 = 向水面提问,激起涟漪;水底有随波折射的"问渠"水印。
 * 无外部依赖;prefers-reduced-motion 时只渲染静水,不做动画。
 */
const cvRef = ref<HTMLCanvasElement | null>(null)

const SW = 480
const SH = 270
const SCALE = 9
const SPEC = 24
const DAMP = 0.975

let hCur = new Float32Array(SW * SH)
let hPrev = new Float32Array(SW * SH)
let bgData: ImageData | null = null
let outImg: ImageData | null = null
let outCanvas: HTMLCanvasElement | null = null
let raf = 0
let autoTimer: number | undefined
let lastMove = { x: -1, y: -1 }

function buildBg(c: CanvasRenderingContext2D) {
  const g = c.createLinearGradient(0, 0, 0, SH)
  g.addColorStop(0, '#0c2f38')
  g.addColorStop(0.55, '#082430')
  g.addColorStop(1, '#03101a')
  c.fillStyle = g
  c.fillRect(0, 0, SW, SH)
  // 天光:几条淡青光带
  for (let i = 0; i < 5; i++) {
    const y = SH * (0.16 + 0.15 * i)
    const lg = c.createLinearGradient(0, y - 16, 0, y + 16)
    lg.addColorStop(0, 'rgba(150,220,205,0)')
    lg.addColorStop(0.5, `rgba(150,220,205,${0.05 - i * 0.007})`)
    lg.addColorStop(1, 'rgba(150,220,205,0)')
    c.fillStyle = lg
    c.fillRect(0, y - 16, SW, 32)
  }
  // 水底若隐若现的"问渠"二字,随涟漪折射晃动
  c.font = '150px KaiTi, "Kaiti SC", "STKaiti", serif'
  c.fillStyle = 'rgba(205,240,228,0.055)'
  c.textAlign = 'center'
  c.textBaseline = 'middle'
  c.fillText('问渠', SW / 2, SH * 0.52)
}

function drop(nx: number, ny: number, r: number, s: number) {
  const cx = (nx * SW) | 0
  const cy = (ny * SH) | 0
  for (let y = -r; y <= r; y++) {
    for (let x = -r; x <= r; x++) {
      const xx = cx + x
      const yy = cy + y
      if (xx < 1 || yy < 1 || xx >= SW - 1 || yy >= SH - 1) continue
      const d2 = x * x + y * y
      if (d2 <= r * r) hPrev[yy * SW + xx] += s * (1 - Math.sqrt(d2) / (r + 1))
    }
  }
}

function stepWater() {
  for (let y = 1; y < SH - 1; y++) {
    const row = y * SW
    for (let x = 1; x < SW - 1; x++) {
      const i = row + x
      const v = (hPrev[i - 1] + hPrev[i + 1] + hPrev[i - SW] + hPrev[i + SW]) / 2 - hCur[i]
      hCur[i] = v * DAMP
    }
  }
  const t = hCur
  hCur = hPrev
  hPrev = t
}

function renderWater(ctx: CanvasRenderingContext2D, cv: HTMLCanvasElement) {
  if (!bgData || !outImg || !outCanvas) return
  const d = outImg.data
  const b = bgData.data
  const p = hPrev
  for (let y = 1; y < SH - 1; y++) {
    const row = y * SW
    for (let x = 1; x < SW - 1; x++) {
      const i = row + x
      const dx = p[i - 1] - p[i + 1]
      const dy = p[i - SW] - p[i + SW]
      let sx = x + ((dx * SCALE) | 0)
      let sy = y + ((dy * SCALE) | 0)
      if (sx < 0) sx = 0
      else if (sx >= SW) sx = SW - 1
      if (sy < 0) sy = 0
      else if (sy >= SH) sy = SH - 1
      const si = (sy * SW + sx) * 4
      const di = i * 4
      const sp = dy * SPEC
      d[di] = b[si] + sp
      d[di + 1] = b[si + 1] + sp
      d[di + 2] = b[si + 2] + sp
      d[di + 3] = 255
    }
  }
  outCanvas.getContext('2d')?.putImageData(outImg, 0, 0)
  ctx.imageSmoothingEnabled = true
  ctx.drawImage(outCanvas, 0, 0, cv.width, cv.height)
}

function onMove(e: MouseEvent) {
  const cv = cvRef.value
  if (!cv) return
  const r = cv.getBoundingClientRect()
  const nx = (e.clientX - r.left) / r.width
  const ny = (e.clientY - r.top) / r.height
  if (Math.abs(nx - lastMove.x) + Math.abs(ny - lastMove.y) < 0.014) return
  lastMove = { x: nx, y: ny }
  drop(nx, ny, 3, 2.2)
}

function onClick(e: MouseEvent) {
  const cv = cvRef.value
  if (!cv) return
  const r = cv.getBoundingClientRect()
  const nx = (e.clientX - r.left) / r.width
  const ny = (e.clientY - r.top) / r.height
  // 点击 = 向水面提问:三重涟漪依次荡开
  drop(nx, ny, 6, 7)
  window.setTimeout(() => drop(nx, ny, 4, 4.5), 160)
  window.setTimeout(() => drop(nx, ny, 2.5, 3), 320)
}

function onResize() {
  const cv = cvRef.value
  if (!cv) return
  cv.width = cv.clientWidth
  cv.height = cv.clientHeight
}

onMounted(() => {
  const cv = cvRef.value
  if (!cv) return
  const ctx = cv.getContext('2d')
  if (!ctx) return

  const bg = document.createElement('canvas')
  bg.width = SW
  bg.height = SH
  const bgCtx = bg.getContext('2d')
  if (!bgCtx) return
  buildBg(bgCtx)
  bgData = bgCtx.getImageData(0, 0, SW, SH)

  outCanvas = document.createElement('canvas')
  outCanvas.width = SW
  outCanvas.height = SH
  outImg = outCanvas.getContext('2d')?.createImageData(SW, SH) ?? null

  onResize()

  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (!reduced) {
    // 开场三滴,让第一眼就"活"(柔和一点,不抢标题)
    window.setTimeout(() => drop(0.38, 0.42, 5, 4), 250)
    window.setTimeout(() => drop(0.62, 0.55, 4.5, 3.5), 700)
    window.setTimeout(() => drop(0.5, 0.32, 4, 3), 1150)
    autoTimer = window.setInterval(() => {
      drop(0.12 + Math.random() * 0.76, 0.18 + Math.random() * 0.55, 4 + ((Math.random() * 3) | 0), 2 + Math.random() * 2.5)
    }, 1600)
  } else {
    stepWater() // 静态兜底:至少渲染一次水面
  }

  const loop = () => {
    stepWater()
    renderWater(ctx, cv)
    raf = requestAnimationFrame(loop)
  }
  raf = requestAnimationFrame(loop)

  window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  cancelAnimationFrame(raf)
  if (autoTimer !== undefined) window.clearInterval(autoTimer)
  window.removeEventListener('resize', onResize)
})
</script>

<template>
  <canvas ref="cvRef" class="water" aria-hidden="true" @mousemove="onMove" @click="onClick" />
  <div class="sheen" aria-hidden="true" />
</template>

<style scoped>
.water {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  z-index: 0;
}
.sheen {
  position: absolute;
  left: 0;
  right: 0;
  top: 64%;
  height: 1px;
  z-index: 1;
  pointer-events: none;
  background: linear-gradient(90deg, transparent, rgba(127, 215, 196, 0.5), transparent);
}
</style>
