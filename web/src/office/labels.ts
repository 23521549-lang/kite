// Text for the 3D office: drawn on a canvas, shown as a sprite that always faces the camera and keeps
// the same size on screen however far the camera is (the scene sets the scale, see sizeLabel).
import * as THREE from 'three'

export type Line = { text: string; color?: string; bold?: boolean; size?: number }
export type LabelStyle = { bg?: string; border?: string; pad?: number; maxWidth?: number; radius?: number }

const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

function fit(ctx: CanvasRenderingContext2D, text: string, max: number): string {
  if (ctx.measureText(text).width <= max) return text
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (ctx.measureText(`${text.slice(0, mid)}…`).width <= max) lo = mid
    else hi = mid - 1
  }
  return `${text.slice(0, lo)}…`
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

/** A sprite with one or more lines of text; `key` lets the caller skip redrawing unchanged labels. */
export function makeLabel(lines: Line[], style: LabelStyle = {}): THREE.Sprite {
  const scale = 2
  const pad = (style.pad ?? 10) * scale
  const maxWidth = (style.maxWidth ?? 300) * scale
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')!
  const fonts = lines.map((l) => `${l.bold ? 600 : 400} ${(l.size ?? 15) * scale}px ${FONT}`)
  const texts = lines.map((l, i) => {
    ctx.font = fonts[i]
    return fit(ctx, l.text, maxWidth)
  })
  const widths = texts.map((t, i) => {
    ctx.font = fonts[i]
    return ctx.measureText(t).width
  })
  const heights = lines.map((l) => (l.size ?? 15) * scale * 1.35)
  canvas.width = Math.ceil(Math.max(...widths, 10) + pad * 2)
  canvas.height = Math.ceil(heights.reduce((a, b) => a + b, 0) + pad * 1.6)
  if (style.bg) {
    roundRect(ctx, 1, 1, canvas.width - 2, canvas.height - 2, (style.radius ?? 10) * scale)
    ctx.fillStyle = style.bg
    ctx.fill()
    if (style.border) {
      ctx.lineWidth = 2 * scale
      ctx.strokeStyle = style.border
      ctx.stroke()
    }
  }
  let y = pad * 0.8
  lines.forEach((l, i) => {
    ctx.font = fonts[i]
    ctx.fillStyle = l.color ?? '#e9ecef'
    ctx.textBaseline = 'top'
    ctx.fillText(texts[i], (canvas.width - widths[i]) / 2, y + (heights[i] - (l.size ?? 15) * scale) / 2.4)
    y += heights[i]
  })
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = 4
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, sizeAttenuation: false })
  const sprite = new THREE.Sprite(material)
  sprite.userData.px = [canvas.width / scale, canvas.height / scale]
  sprite.scale.set(0.0001, 0.0001, 1)
  sprite.renderOrder = 10
  return sprite
}

/** Size in CSS pixels of a label made by makeLabel. */
export function labelSize(sprite: THREE.Sprite): [number, number] {
  return (sprite.userData.px as [number, number] | undefined) ?? [0, 0]
}

/** Scale a label so one canvas pixel is one screen pixel; `unit` is the sprite scale of one pixel. */
export function sizeLabel(sprite: THREE.Sprite, unit: number) {
  const [w, h] = labelSize(sprite)
  if (w) sprite.scale.set(w * unit, h * unit, 1)
}

export function disposeSprite(sprite: THREE.Sprite) {
  sprite.material.map?.dispose()
  sprite.material.dispose()
}
