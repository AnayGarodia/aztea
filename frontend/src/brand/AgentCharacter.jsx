import './AgentCharacter.css'

const SCREEN_BG  = '#0d1525'
const SCREEN_FG  = 'white'
const FACE_COLOR = '#1a1a2e'

// ── 5-point star helper ───────────────────────────────────────
function StarMark({ cx, cy }) {
  const R = 6, r = 2.4
  const pts = []
  for (let i = 0; i < 5; i++) {
    const oa = (i * 72 - 90) * Math.PI / 180
    const ia = (i * 72 - 54) * Math.PI / 180
    pts.push(`${(cx + R * Math.cos(oa)).toFixed(1)},${(cy + R * Math.sin(oa)).toFixed(1)}`)
    pts.push(`${(cx + r * Math.cos(ia)).toFixed(1)},${(cy + r * Math.sin(ia)).toFixed(1)}`)
  }
  return <polygon points={pts.join(' ')} fill={SCREEN_FG} />
}

// ── Eye Screens ───────────────────────────────────────────────
// Left screen:  x=21 y=17 w=26 h=22  center=(34,28)
// Right screen: x=53 y=17 w=26 h=22  center=(66,28)
function EyeScreens({ eyeShape, state }) {
  let expr

  if (state === 'working') {
    // Squinting horizontal bars
    expr = (
      <g>
        <rect x="25" y="25" width="18" height="6" rx="3" fill={SCREEN_FG} />
        <rect x="57" y="25" width="18" height="6" rx="3" fill={SCREEN_FG} />
      </g>
    )
  } else if (state === 'celebrating') {
    // Happy closed arcs (∩-shaped = squinting up in happiness)
    expr = (
      <g>
        <path d="M24,31 Q34,19 44,31" stroke={SCREEN_FG} strokeWidth="3.5" fill="none" strokeLinecap="round" />
        <path d="M56,31 Q66,19 76,31" stroke={SCREEN_FG} strokeWidth="3.5" fill="none" strokeLinecap="round" />
      </g>
    )
  } else if (state === 'dejected') {
    // Droopy downward arcs (∪-shaped)
    expr = (
      <g>
        <path d="M24,24 Q34,34 44,24" stroke={SCREEN_FG} strokeWidth="3" fill="none" strokeLinecap="round" />
        <path d="M56,24 Q66,34 76,24" stroke={SCREEN_FG} strokeWidth="3" fill="none" strokeLinecap="round" />
      </g>
    )
  } else {
    // idle — vary by eyeShape trait
    if (eyeShape === 'square') {
      expr = (
        <g>
          <rect x="29" y="23" width="10" height="10" rx="1.5" fill={SCREEN_FG} />
          <rect x="61" y="23" width="10" height="10" rx="1.5" fill={SCREEN_FG} />
        </g>
      )
    } else if (eyeShape === 'star') {
      expr = (
        <g>
          <StarMark cx={34} cy={28} />
          <StarMark cx={66} cy={28} />
        </g>
      )
    } else {
      // round (default)
      expr = (
        <g>
          <circle cx="34" cy="28" r="6" fill={SCREEN_FG} />
          <circle cx="66" cy="28" r="6" fill={SCREEN_FG} />
        </g>
      )
    }
  }

  return (
    <g>
      <rect x="21" y="17" width="26" height="22" rx="5" fill={SCREEN_BG} />
      <rect x="53" y="17" width="26" height="22" rx="5" fill={SCREEN_BG} />
      {expr}
    </g>
  )
}

// ── Mouth ─────────────────────────────────────────────────────
function Mouth({ mouthShape, state }) {
  const arc = { stroke: FACE_COLOR, strokeWidth: '3', fill: 'none', strokeLinecap: 'round' }

  if (state === 'celebrating') {
    return <path d="M36,51 Q50,64 64,51" fill={FACE_COLOR} />
  }
  if (state === 'dejected') {
    return <path d="M38,58 Q50,50 62,58" {...arc} />
  }
  if (state === 'working') {
    return <line x1="40" y1="55" x2="60" y2="55" stroke={FACE_COLOR} strokeWidth="3" strokeLinecap="round" />
  }
  switch (mouthShape) {
    case 'excited':
      return <path d="M36,51 Q50,64 64,51" fill={FACE_COLOR} />
    case 'determined':
      return <line x1="40" y1="55" x2="60" y2="55" stroke={FACE_COLOR} strokeWidth="3" strokeLinecap="round" />
    case 'flat':
      return <line x1="43" y1="55" x2="57" y2="55" stroke={FACE_COLOR} strokeWidth="2.5" strokeLinecap="round" />
    case 'smile':
    default:
      return <path d="M38,52 Q50,62 62,52" {...arc} />
  }
}

// ── Side panels / ears ────────────────────────────────────────
function Ears({ earShape, bodyColor }) {
  switch (earShape) {
    case 'pointed':
      // Narrow rectangular panel tabs
      return (
        <g>
          <rect x="1" y="28" width="12" height="22" rx="2" fill={bodyColor} />
          <rect x="87" y="28" width="12" height="22" rx="2" fill={bodyColor} />
        </g>
      )
    case 'floppy':
      // Wide ear flaps
      return (
        <g>
          <rect x="0" y="20" width="14" height="34" rx="5" fill={bodyColor} />
          <rect x="86" y="20" width="14" height="34" rx="5" fill={bodyColor} />
        </g>
      )
    case 'round':
    default:
      // Circular sensor bumps
      return (
        <g>
          <circle cx="11" cy="37" r="10" fill={bodyColor} />
          <circle cx="89" cy="37" r="10" fill={bodyColor} />
        </g>
      )
  }
}

// ── Arms (thick, bent at elbow) ───────────────────────────────
function Arms({ state, bodyColor }) {
  const sp = { stroke: bodyColor, strokeWidth: '11', strokeLinecap: 'round', strokeLinejoin: 'round', fill: 'none' }

  switch (state) {
    case 'working':
      // Right arm raised up, left relaxed
      return (
        <g>
          <polyline points="29,80 16,75 10,88" {...sp} />
          <polyline points="71,78 84,65 91,57" {...sp} />
        </g>
      )
    case 'celebrating':
      // Both arms raised
      return (
        <g>
          <polyline points="29,78 16,67 10,57" {...sp} />
          <polyline points="71,78 84,67 90,57" {...sp} />
        </g>
      )
    case 'dejected':
      // Arms drooping down
      return (
        <g>
          <polyline points="29,82 18,91 13,101" {...sp} />
          <polyline points="71,82 82,91 87,101" {...sp} />
        </g>
      )
    case 'idle':
    default:
      return (
        <g>
          <polyline points="29,80 16,75 10,88" {...sp} />
          <polyline points="71,80 84,75 90,88" {...sp} />
        </g>
      )
  }
}

// ── Legs ──────────────────────────────────────────────────────
function Legs({ state, bodyColor }) {
  const sp = { stroke: bodyColor, strokeWidth: '10', strokeLinecap: 'round', fill: 'none' }

  if (state === 'celebrating') {
    return (
      <g>
        <line x1="42" y1="100" x2="37" y2="112" {...sp} />
        <line x1="58" y1="100" x2="63" y2="112" {...sp} />
      </g>
    )
  }
  return (
    <g>
      <line x1="42" y1="100" x2="40" y2="112" {...sp} />
      <line x1="58" y1="100" x2="60" y2="112" {...sp} />
    </g>
  )
}

// ── Accessory ─────────────────────────────────────────────────
function Accessory({ accessory }) {
  switch (accessory) {
    case 'hat':
      return (
        <g>
          <rect x="22" y="0" width="56" height="10" rx="3" fill="#333344" />
          <rect x="16" y="7" width="68" height="6" rx="3" fill="#333344" />
        </g>
      )
    case 'antenna':
      return (
        <g>
          <line x1="50" y1="4" x2="50" y2="-9" stroke="#555566" strokeWidth="3" strokeLinecap="round" />
          <circle cx="50" cy="-13" r="5" fill="#FFC800" />
        </g>
      )
    case 'headphones':
      return (
        <g>
          <path d="M18,30 Q18,0 50,0 Q82,0 82,30" stroke="#333344" strokeWidth="5" fill="none" strokeLinecap="round" />
          <rect x="9" y="25" width="13" height="16" rx="6" fill="#333344" />
          <rect x="78" y="25" width="13" height="16" rx="6" fill="#333344" />
        </g>
      )
    case 'glasses':
      // Visor overlay over eye area
      return (
        <g>
          <rect x="17" y="14" width="66" height="28" rx="8" fill="rgba(100,180,255,0.15)" stroke="#888899" strokeWidth="1.5" />
          <line x1="17" y1="28" x2="83" y2="28" stroke="#888899" strokeWidth="1" opacity="0.4" />
        </g>
      )
    case 'none':
    default:
      return null
  }
}

// ── Main component ────────────────────────────────────────────
/**
 * AgentCharacter SVG mascot — chunky robot with rounded-square head and screen eyes.
 * ViewBox 0 0 100 115.  size prop = height; width is auto-computed.
 * state: 'idle' | 'working' | 'celebrating' | 'dejected'
 */
export default function AgentCharacter({
  state      = 'idle',
  bodyColor  = '#58CC02',
  eyeShape   = 'round',
  accessory  = 'none',
  earShape   = 'round',
  mouthShape = 'smile',
  size       = 80,
  animDelay  = 0,
}) {
  const w = Math.round(size * (100 / 115))

  const animClass = state === 'idle'        ? 'char-float'
                  : state === 'celebrating' ? 'char-bounce'
                  : ''

  return (
    <div
      className={`agent-char ${animClass}`}
      style={{ width: w, height: size, flexShrink: 0, animationDelay: `${animDelay}s` }}
      aria-hidden="true"
    >
      <svg
        viewBox="0 0 100 115"
        width={w}
        height={size}
        overflow="visible"
        xmlns="http://www.w3.org/2000/svg"
      >
        {/* Side panels / ears — rendered behind head */}
        <Ears earShape={earShape} bodyColor={bodyColor} />

        {/* Legs — behind body */}
        <Legs state={state} bodyColor={bodyColor} />

        {/* Arms — behind body */}
        <Arms state={state} bodyColor={bodyColor} />

        {/* Head — chunky rounded square */}
        <rect x="11" y="4" width="78" height="62" rx="18" fill={bodyColor} />

        {/* Soft highlight on head */}
        <rect x="18" y="9" width="28" height="18" rx="9" fill="rgba(255,255,255,0.22)" />

        {/* Eye screens + state-driven expressions */}
        <EyeScreens eyeShape={eyeShape} state={state} />

        {/* Mouth */}
        <Mouth mouthShape={mouthShape} state={state} />

        {/* Body */}
        <rect x="29" y="68" width="42" height="32" rx="10" fill={bodyColor} />

        {/* Body LED indicator */}
        <circle cx="50" cy="84" r="3.5" fill="rgba(0,0,0,0.18)" />
        <circle cx="50" cy="84" r="2"   fill="rgba(255,255,255,0.28)" />

        {/* Accessory — always rendered last / on top */}
        <Accessory accessory={accessory} />
      </svg>
    </div>
  )
}
