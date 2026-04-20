// JS mirrors of CSS motion tokens so primitives don't hardcode easing arrays.
// framer-motion can't read CSS custom properties directly.

export const easeExpo  = [0.16, 1, 0.3, 1]
export const easeBack  = [0.34, 1.56, 0.64, 1]
export const easeIn    = [0.4, 0, 1, 1]
export const easeOut   = [0, 0, 0.2, 1]

export const durXs = 0.08
export const durSm = 0.15
export const durMd = 0.25
export const durLg = 0.4

export const spring = { type: 'spring', stiffness: 400, damping: 40, mass: 0.8 }
export const springBouncy = { type: 'spring', bounce: 0.35, duration: 0.5 }
export const springTight  = { type: 'spring', stiffness: 500, damping: 35, mass: 0.6 }
