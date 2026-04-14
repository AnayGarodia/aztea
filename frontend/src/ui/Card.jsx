import './Card.css'

export default function Card({
  variant = '',
  interactive = false,
  className = '',
  children,
  onClick,
  ...props
}) {
  const cls = [
    'card',
    variant ? `card--${variant}` : '',
    interactive ? 'card--interactive' : '',
    className,
  ].filter(Boolean).join(' ')

  return (
    <div className={cls} onClick={onClick} {...props}>
      {children}
    </div>
  )
}

Card.Header = function CardHeader({ children, className = '', ...props }) {
  return <div className={`card__header ${className}`} {...props}>{children}</div>
}
Card.Body = function CardBody({ children, className = '', ...props }) {
  return <div className={`card__body ${className}`} {...props}>{children}</div>
}
Card.Footer = function CardFooter({ children, className = '', ...props }) {
  return <div className={`card__footer ${className}`} {...props}>{children}</div>
}
