import { useEffect, useRef } from 'react'
import './GradientBeams.css'

export default function GradientBeams({ className }) {
  return (
    <div className={`grad-beams ${className ?? ''}`} aria-hidden>
      <div className="grad-beams__beam grad-beams__beam--1" />
      <div className="grad-beams__beam grad-beams__beam--2" />
      <div className="grad-beams__beam grad-beams__beam--3" />
      <div className="grad-beams__orb grad-beams__orb--1" />
      <div className="grad-beams__orb grad-beams__orb--2" />
    </div>
  )
}
