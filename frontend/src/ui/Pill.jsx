import './Pill.css'

export default function Pill({ active, interactive, size, children, className = '', ...props }) {
  return (
    <span
      className={[
        'pill',
        interactive ? 'pill--interactive' : '',
        active ? 'pill--active' : '',
        size ? `pill--${size}` : '',
        className,
      ].filter(Boolean).join(' ')}
      {...props}
    >
      {children}
    </span>
  )
}
