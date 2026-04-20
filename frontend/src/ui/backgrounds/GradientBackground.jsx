import { GrainGradient } from '@paper-design/shaders-react'
import './GradientBackground.css'

const LIGHT_THEME = {
  colorBack: 'hsl(42, 34%, 92%)',
  colors: ['hsl(38, 62%, 56%)', 'hsl(24, 78%, 62%)', 'hsl(332, 60%, 60%)'],
  softness: 0.82,
  intensity: 0.34,
  noise: 0.02,
  speed: 0.32,
}

const DARK_THEME = {
  colorBack: 'hsl(225, 18%, 10%)',
  colors: ['hsl(37, 70%, 52%)', 'hsl(332, 64%, 50%)', 'hsl(265, 64%, 58%)'],
  softness: 0.78,
  intensity: 0.42,
  noise: 0.05,
  speed: 0.4,
}

export default function GradientBackground({ isDark = false, className = '' }) {
  const palette = isDark ? DARK_THEME : LIGHT_THEME
  const cls = ['gradient-background', className].filter(Boolean).join(' ')

  return (
    <div className={cls} aria-hidden>
      <GrainGradient
        style={{ width: '100%', height: '100%' }}
        colorBack={palette.colorBack}
        softness={palette.softness}
        intensity={palette.intensity}
        noise={palette.noise}
        shape="corners"
        offsetX={0}
        offsetY={0}
        scale={1}
        rotation={0}
        speed={palette.speed}
        colors={palette.colors}
      />
      <div className="gradient-background__veil" />
    </div>
  )
}
