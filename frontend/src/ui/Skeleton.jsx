import './Skeleton.css'

export default function Skeleton({ variant = 'text', width, height, className = '', style }) {
  return (
    <div
      className={`skeleton skeleton--${variant} ${className}`}
      style={{ width, height, ...style }}
    />
  )
}
