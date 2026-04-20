import './Shimmer.css'

export default function Shimmer({ children, className = '', style }) {
  return (
    <span className={`shimmer-wrap ${className}`} style={style}>
      {children}
      <span className="shimmer-sweep" aria-hidden />
    </span>
  )
}
