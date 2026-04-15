// ── Deterministic hash (djb2 variant) ────────────────────────
function hashStr(str) {
  let h = 5381
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) + h) ^ str.charCodeAt(i)
    h = h | 0  // keep 32-bit
  }
  return Math.abs(h)
}

export const BODY_COLORS = ['#58CC02', '#CE82FF', '#1CB0F6', '#FFC800', '#FF4B4B']
export const EYE_SHAPES  = ['round', 'square', 'star']
export const ACCESSORIES = ['none', 'hat', 'antenna', 'headphones', 'glasses']
export const EAR_SHAPES  = ['round', 'pointed', 'floppy']
export const MOUTH_SHAPES = ['smile', 'determined', 'excited', 'flat']

/**
 * generateAgentCharacter(agentId)
 * Deterministically maps an agent's ID string to a set of visual traits.
 * The same ID always produces the same traits; different IDs look noticeably
 * different because each trait uses a separate slice of the hash bits.
 */
export function generateAgentCharacter(agentId) {
  const h = hashStr(String(agentId))
  return {
    bodyColor:  BODY_COLORS[ h         % 5],
    eyeShape:   EYE_SHAPES [ (h >>> 4) % 3],
    accessory:  ACCESSORIES[ (h >>> 8) % 5],
    earShape:   EAR_SHAPES [ (h >>> 12) % 3],
    mouthShape: MOUTH_SHAPES[ (h >>> 16) % 4],
  }
}
