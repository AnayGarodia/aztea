// Deterministic hash (djb2 variant)
function djb2(str) {
  let h = 5381
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) + h) ^ str.charCodeAt(i)
    h = h | 0
  }
  return Math.abs(h)
}

// Seeded PRNG
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = seed + 0x6D2B79F5 | 0
    let t = Math.imul(seed ^ seed >>> 15, 1 | seed)
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t
    return ((t ^ t >>> 14) >>> 0) / 4294967296
  }
}

const GRADIENT_PAIRS = [
  ['#5EF3A3', '#22C88B'],
  ['#A78BFA', '#7C3AED'],
  ['#60A5FA', '#3B82F6'],
  ['#F472B6', '#EC4899'],
  ['#34D399', '#6EE7B7'],
  ['#FBBF24', '#F59E0B'],
  ['#5EF3A3', '#60A5FA'],
  ['#A78BFA', '#F472B6'],
  ['#22C88B', '#3B82F6'],
  ['#F87171', '#FBBF24'],
]

const SHAPES = ['orbit', 'hex', 'prism', 'mesh', 'spiral', 'ring', 'diamond', 'cross']

export function getSigilTraits(agentId) {
  const seed = djb2(String(agentId))
  const rand = mulberry32(seed)

  const shapeIdx = Math.floor(rand() * SHAPES.length)
  const gradIdx  = Math.floor(rand() * GRADIENT_PAIRS.length)
  const rotation = Math.floor(rand() * 360)
  const strokeW  = 1 + rand() * 1.5

  return {
    shape:    SHAPES[shapeIdx],
    colors:   GRADIENT_PAIRS[gradIdx],
    rotation,
    strokeW,
    gradId:   `sigil-grad-${seed}`,
  }
}
