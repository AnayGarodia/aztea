function djb2(str) {
  let h = 5381
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) + h) ^ str.charCodeAt(i)
    h = h | 0
  }
  return Math.abs(h)
}

function mulberry32(seed) {
  return function () {
    seed |= 0; seed = seed + 0x6D2B79F5 | 0
    let t = Math.imul(seed ^ seed >>> 15, 1 | seed)
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t
    return ((t ^ t >>> 14) >>> 0) / 4294967296
  }
}

export function getSigilTraits(agentId) {
  const seed = djb2(String(agentId))
  const rand = mulberry32(seed)

  const baseHue = Math.floor(rand() * 360)

  const colors = [
    `hsl(${baseHue}, 68%, 44%)`,
    `hsl(${(baseHue + 55)  % 360}, 64%, 56%)`,
    `hsl(${(baseHue + 170) % 360}, 58%, 52%)`,
    `hsl(${(baseHue + 220) % 360}, 52%, 62%)`,
    `hsl(${baseHue}, 78%, 32%)`,
  ]

  return {
    seed,
    colors,
    primaryColor: colors[0],
    // Legacy compat
    shape:    'bauhaus',
    rotation: 0,
    strokeW:  2,
    gradId:   `sigil-grad-${seed}`,
  }
}

export function getAgentColor(agentId) {
  return getSigilTraits(agentId).primaryColor
}
