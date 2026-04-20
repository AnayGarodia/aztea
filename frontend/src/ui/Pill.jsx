import './Pill.css'

export default function Pill({ active, interactive, size, children, className = '', ...props }) {
  const classes = [
    'pill',
    interactive ? 'pill--interactive' : '',
    active ? 'pill--active' : '',
    size ? `pill--${size}` : '',
    className,
  ].filter(Boolean).join(' ')

  if (interactive) {
    return (
      <button
        type={props.type ?? 'button'}
        className={classes}
        {...props}
      >
        {children}
      </button>
    )
  }

  return (
    <span className={classes} {...props}>
      {children}
    </span>
  )
}
