import './Avatar.css'

export default function Avatar({ name = '?', size = 'md', className = '', ...props }) {
  const initial = String(name).trim()[0]?.toUpperCase() ?? '?'
  return (
    <span className={`avatar avatar--${size} ${className}`} aria-label={name} {...props}>
      {initial}
    </span>
  )
}
