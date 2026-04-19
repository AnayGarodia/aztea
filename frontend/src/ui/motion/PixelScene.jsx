import { useEffect, useRef } from 'react'
import './PixelScene.css'

// ── Canvas resolution (1/10 of 1080p, 16:9) ─────────────────
const W = 192
const H = 108

// ── Color constants per theme ────────────────────────────────────
const C_DARK = {
  bg:           '#08090C',
  grid:         'rgba(28,32,48,0.28)',
  ground:       'rgba(28,32,48,0.55)',
  emerald:      '#5EF3A3',
  emeraldDk:    '#22C88B',
  violet:       '#A78BFA',
  violetDk:     '#7C3AED',
  white:        '#E8ECF4',
  edge:         'rgba(8,9,12,0.5)',
  edgeRgb:      '8,9,12',
}

const C_LIGHT = {
  bg:           '#F5F2EB',
  grid:         'rgba(140,120,90,0.12)',
  ground:       'rgba(140,120,90,0.28)',
  emerald:      '#16A34A',
  emeraldDk:    '#14532D',
  violet:       '#7C3AED',
  violetDk:     '#5B21B6',
  white:        '#1C1917',
  edge:         'rgba(245,242,235,0.5)',
  edgeRgb:      '245,242,235',
}

function getThemeColors() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? C_LIGHT : C_DARK
}

// Mutable C reference — updated on theme change
let C = getThemeColors()

// ── Agent sprite: 7w × 11h ────────────────────────────────────
// 0 = transparent  1 = primary  2 = dark shade  3 = white (eyes)
const SPRITE = [
  [0, 0, 1, 1, 1, 0, 0],  // head top
  [0, 1, 3, 1, 3, 1, 0],  // face + eyes
  [0, 1, 1, 1, 1, 1, 0],  // head bottom
  [0, 0, 2, 2, 2, 0, 0],  // neck
  [0, 2, 1, 1, 1, 2, 0],  // shoulders
  [0, 0, 1, 1, 1, 0, 0],  // torso mid
  [0, 0, 1, 1, 1, 0, 0],  // torso bottom
  [0, 0, 2, 0, 2, 0, 0],  // hips
  [0, 0, 2, 0, 2, 0, 0],  // upper leg
  [0, 0, 2, 0, 2, 0, 0],  // lower leg
  [0, 2, 2, 0, 2, 2, 0],  // feet
]

// Mirrored (Agent B faces left)
const SPRITE_MIR = SPRITE.map(row => [...row].reverse())

// ── Sprite palette sets ───────────────────────────────────────
const PAL_A = { 1: C.emerald, 2: C.emeraldDk, 3: C.white }
const PAL_B = { 1: C.violet,  2: C.violetDk,  3: C.white }

// ── Agent & packet positions ──────────────────────────────────
const AX = 28   // Agent A top-left x
const BX = 157  // Agent B top-left x (sprite 7px wide → occupies 157-163)
const AY = 56   // Agent top y (feet land on ground at y=67)
const BY = 56
const GROUND_Y = 67

const PKT_AX = 42   // packet launch x (just right of Agent A's arm)
const PKT_BX = 150  // packet arrival x (just left of Agent B's arm)
const PKT_Y  = 60   // packet y (mid-chest height)

// ── Animation phase boundaries (ms) ──────────────────────────
const PH = {
  IDLE:      1000,
  CREATE:    1500,
  TRANSIT:   3500,
  RECEIPT:   4500,
  CELEBRATE: 5500,
  TOTAL:     6000,
}

// ── Utilities ─────────────────────────────────────────────────
function prefersReducedMotion() {
  return typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function easeInOut(t) {
  return t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t
}

function lerp(a, b, t) { return a + (b - a) * t }

// Interpolate between two '#rrggbb' strings
function lerpHex(c1, c2, t) {
  const r1 = parseInt(c1.slice(1, 3), 16)
  const g1 = parseInt(c1.slice(3, 5), 16)
  const b1 = parseInt(c1.slice(5, 7), 16)
  const r2 = parseInt(c2.slice(1, 3), 16)
  const g2 = parseInt(c2.slice(3, 5), 16)
  const b2 = parseInt(c2.slice(5, 7), 16)
  return `rgb(${Math.round(lerp(r1,r2,t))},${Math.round(lerp(g1,g2,t))},${Math.round(lerp(b1,b2,t))})`
}

// ── Drawing primitives ────────────────────────────────────────
function drawSprite(ctx, sprite, palette, x, y, flash = 0) {
  const ox = Math.round(x)
  const oy = Math.round(y)
  for (let row = 0; row < sprite.length; row++) {
    for (let col = 0; col < sprite[row].length; col++) {
      const v = sprite[row][col]
      if (!v) continue
      if (flash > 0) {
        // Blend towards white during flash
        ctx.fillStyle = `rgba(232,236,244,${flash})`
        ctx.fillRect(ox + col, oy + row, 1, 1)
      }
      ctx.fillStyle = palette[v]
      ctx.globalAlpha = flash > 0 ? 1 - flash * 0.5 : 1
      ctx.fillRect(ox + col, oy + row, 1, 1)
      ctx.globalAlpha = 1
    }
  }
}

function drawBackground(ctx) {
  // Base fill
  ctx.fillStyle = C.bg
  ctx.fillRect(0, 0, W, H)

  // Grid
  ctx.fillStyle = C.grid
  for (let x = 0; x < W; x += 12) ctx.fillRect(x, 0, 1, H)
  for (let y = 0; y < H; y += 12) ctx.fillRect(0, y, W, 1)

  // Ground line
  ctx.fillStyle = C.ground
  ctx.fillRect(0, GROUND_Y, W, 1)

  // Edge vignette — top/bottom
  for (let i = 0; i < 14; i++) {
    const a = ((14 - i) / 14) * 0.55
    ctx.fillStyle = `rgba(${C.edgeRgb},${a})`
    ctx.fillRect(0, i, W, 1)
    ctx.fillRect(0, H - 1 - i, W, 1)
  }
  // Edge vignette — left/right
  for (let i = 0; i < 18; i++) {
    const a = ((18 - i) / 18) * 0.45
    ctx.fillStyle = `rgba(${C.edgeRgb},${a})`
    ctx.fillRect(i, 0, 1, H)
    ctx.fillRect(W - 1 - i, 0, 1, H)
  }
}

function drawPacket(ctx, x, y, color, gr, gg, gb, glowAlpha) {
  const cx = Math.round(x)
  const cy = Math.round(y)
  // Outer glow (7×7)
  ctx.fillStyle = `rgba(${gr},${gg},${gb},${glowAlpha})`
  ctx.fillRect(cx - 3, cy - 3, 7, 7)
  // Core (3×3)
  ctx.fillStyle = color
  ctx.fillRect(cx - 1, cy - 1, 3, 3)
}

// ── Particle systems ──────────────────────────────────────────
function makeAmbientParticles() {
  const pts = []
  for (let i = 0; i < 14; i++) {
    pts.push({
      x:       Math.random() * W,
      y:       Math.random() * H,
      vy:      -(0.04 + Math.random() * 0.08),
      violet:  i >= 7,
      alpha:   0.06 + Math.random() * 0.07,
    })
  }
  return pts
}

function tickAmbient(pts) {
  for (const p of pts) {
    p.y += p.vy
    if (p.y < 0) { p.y = H; p.x = Math.random() * W }
  }
}

function drawAmbient(ctx, pts) {
  for (const p of pts) {
    ctx.fillStyle = p.violet
      ? `rgba(167,139,250,${p.alpha})`
      : `rgba(94,243,163,${p.alpha})`
    ctx.fillRect(Math.floor(p.x), Math.floor(p.y), 1, 1)
  }
}

function makeBurst(x, y) {
  const pts = []
  for (let i = 0; i < 10; i++) {
    const angle = (i / 10) * Math.PI * 2
    pts.push({
      x,
      y,
      vx: Math.cos(angle) * (0.25 + Math.random() * 0.35),
      vy: Math.sin(angle) * (0.25 + Math.random() * 0.35),
      life: 0.9 + Math.random() * 0.1,
    })
  }
  return pts
}

function tickBurst(pts) {
  for (const p of pts) { p.x += p.vx; p.y += p.vy; p.life -= 0.05 }
  return pts.filter(p => p.life > 0)
}

function drawBurst(ctx, pts) {
  for (const p of pts) {
    ctx.fillStyle = `rgba(167,139,250,${p.life * 0.85})`
    ctx.fillRect(Math.floor(p.x), Math.floor(p.y), 1, 1)
  }
}

// ── Static frame (prefers-reduced-motion) ─────────────────────
function drawStatic(ctx, ambient) {
  drawBackground(ctx)
  drawAmbient(ctx, ambient)
  drawSprite(ctx, SPRITE,     PAL_A, AX, AY)
  drawSprite(ctx, SPRITE_MIR, PAL_B, BX, BY)
  // Packet at midpoint, blended color
  const mx = (PKT_AX + PKT_BX) / 2
  drawPacket(ctx, mx, PKT_Y, '#84BFD0', 132, 191, 208, 0.22)
}

// ── Main component ────────────────────────────────────────────
export default function PixelScene({ className = '' }) {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const ctx = canvas.getContext('2d', { alpha: false })
    ctx.imageSmoothingEnabled = false

    // Keep C in sync with theme toggle
    C = getThemeColors()
    const themeObserver = new MutationObserver(() => { C = getThemeColors() })
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })

    const ambient = makeAmbientParticles()

    if (prefersReducedMotion()) {
      drawStatic(ctx, ambient)
      return () => themeObserver.disconnect()
    }

    let rafId  = null
    let elapsed  = 0
    let lastNow  = null
    let prevT    = 0
    let burstFired = false
    let burst    = []

    function tick(now) {
      if (lastNow === null) lastNow = now
      const delta = Math.min(now - lastNow, 50)
      lastNow = now
      elapsed += delta

      const t = elapsed % PH.TOTAL

      // Detect loop wrap-around → reset burst state
      if (t < prevT) { burstFired = false; burst = [] }
      prevT = t

      // Tick particles
      tickAmbient(ambient)

      // === Draw background ===
      drawBackground(ctx)
      drawAmbient(ctx, ambient)

      // === Agent breathing offset (1px bob, period ~1.8s) ===
      const breathA = Math.sin(elapsed / 450) > 0 ? 0 : 1
      const breathB = Math.sin(elapsed / 450 + Math.PI) > 0 ? 0 : 1

      // === Phase logic ===
      if (t < PH.IDLE) {
        // Idle: agents breathe
        drawSprite(ctx, SPRITE,     PAL_A, AX, AY + breathA)
        drawSprite(ctx, SPRITE_MIR, PAL_B, BX, BY + breathB)

      } else if (t < PH.CREATE) {
        // Task creation: packet materialises at Agent A's hand
        const p = (t - PH.IDLE) / (PH.CREATE - PH.IDLE)
        const size = Math.max(1, Math.round(p * 3))
        const half = Math.floor(size / 2)
        const ga = p * 0.25

        drawSprite(ctx, SPRITE,     PAL_A, AX, AY + breathA)
        drawSprite(ctx, SPRITE_MIR, PAL_B, BX, BY + breathB)

        ctx.fillStyle = `rgba(94,243,163,${ga})`
        ctx.fillRect(PKT_AX - 3, PKT_Y - 3, 7, 7)
        ctx.fillStyle = C.emerald
        ctx.fillRect(PKT_AX - half, PKT_Y - half, size, size)

      } else if (t < PH.TRANSIT) {
        // Transit: packet moves A → B
        const p = (t - PH.CREATE) / (PH.TRANSIT - PH.CREATE)
        const eased = easeInOut(p)
        const px = lerp(PKT_AX, PKT_BX, eased)

        // Colour shifts emerald → violet
        const color = lerpHex(C.emerald, C.violet, eased)
        const gr = Math.round(lerp(94, 167, eased))
        const gg = Math.round(lerp(243, 139, eased))
        const gb = Math.round(lerp(163, 250, eased))
        const pulse = 0.12 + 0.08 * Math.sin(elapsed / 100)

        // Trail
        for (let tr = 1; tr <= 5; tr++) {
          const tp = Math.max(0, p - tr * 0.012)
          const te = easeInOut(tp)
          const tx = lerp(PKT_AX, PKT_BX, te)
          const ta = Math.max(0, (0.06 - tr * 0.011) * p)
          ctx.fillStyle = `rgba(${gr},${gg},${gb},${ta})`
          ctx.fillRect(Math.round(tx) - 1, PKT_Y - 1, 3, 3)
        }

        drawSprite(ctx, SPRITE,     PAL_A, AX, AY + breathA)
        drawSprite(ctx, SPRITE_MIR, PAL_B, BX, BY + breathB)
        drawPacket(ctx, px, PKT_Y, color, gr, gg, gb, pulse)

      } else if (t < PH.RECEIPT) {
        // Receipt: packet absorbed, burst fires
        const p = (t - PH.TRANSIT) / (PH.RECEIPT - PH.TRANSIT)

        drawSprite(ctx, SPRITE,     PAL_A, AX, AY + breathA)
        drawSprite(ctx, SPRITE_MIR, PAL_B, BX, BY + breathB)

        if (!burstFired) {
          burst = makeBurst(PKT_BX, PKT_Y)
          burstFired = true
        }

        // Shrinking packet
        if (p < 0.15) {
          const size = Math.max(1, Math.round(3 * (1 - p / 0.15)))
          ctx.fillStyle = C.violet
          ctx.fillRect(PKT_BX - 1, PKT_Y - 1, size, size)
        }

        // Violet glow behind Agent B fades in then out
        const glowA = Math.max(0, Math.sin(p * Math.PI) * 0.3)
        ctx.fillStyle = `rgba(167,139,250,${glowA})`
        ctx.fillRect(BX - 2, BY - 2, 11, 15)

        // Burst
        burst = tickBurst(burst)
        drawBurst(ctx, burst)

      } else if (t < PH.CELEBRATE) {
        // Celebration: sparkles + flash
        const p = (t - PH.RECEIPT) / (PH.CELEBRATE - PH.RECEIPT)
        const flash = Math.max(0, Math.sin(p * Math.PI * 2.5) * 0.18)

        drawSprite(ctx, SPRITE,     PAL_A, AX, AY + breathA, flash)
        drawSprite(ctx, SPRITE_MIR, PAL_B, BX, BY + breathB, flash)

        // Sparkle pixels near each agent
        for (let s = 0; s < 2; s++) {
          if (Math.random() > 0.55) {
            ctx.fillStyle = `rgba(94,243,163,${0.3 + Math.random() * 0.5})`
            ctx.fillRect(AX + Math.floor(Math.random() * 9), AY - 2 + Math.floor(Math.random() * 5), 1, 1)
          }
          if (Math.random() > 0.55) {
            ctx.fillStyle = `rgba(167,139,250,${0.3 + Math.random() * 0.5})`
            ctx.fillRect(BX + Math.floor(Math.random() * 9), BY - 2 + Math.floor(Math.random() * 5), 1, 1)
          }
        }

      } else {
        // Reset (5.5s–6s): gentle fade back to idle
        const p = (t - PH.CELEBRATE) / (PH.TOTAL - PH.CELEBRATE)
        drawSprite(ctx, SPRITE,     PAL_A, AX, AY + breathA)
        drawSprite(ctx, SPRITE_MIR, PAL_B, BX, BY + breathB)
        // Subtle overlay fades out (0→1)
        ctx.fillStyle = `rgba(${C.edgeRgb},${(1 - p) * 0.15})`
        ctx.fillRect(0, 0, W, H)
      }

      rafId = requestAnimationFrame(tick)
    }

    rafId = requestAnimationFrame(tick)
    return () => {
      if (rafId !== null) cancelAnimationFrame(rafId)
      themeObserver.disconnect()
    }
  }, [])

  return (
    <div className={`pixel-scene ${className}`} aria-hidden="true">
      <canvas
        ref={canvasRef}
        className="pixel-scene__canvas"
        width={W}
        height={H}
      />
    </div>
  )
}
