import { useEffect } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { X } from 'lucide-react'
import './Dialog.css'

export default function Dialog({ open, onClose, title, size, children }) {
  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="dialog-overlay"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
          onClick={(e) => { if (e.target === e.currentTarget) onClose?.() }}
        >
          <motion.div
            className={`dialog ${size ? `dialog--${size}` : ''}`}
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
          >
            <div className="dialog__header">
              <h2 className="dialog__title">{title}</h2>
              {onClose && (
                <button className="dialog__close" onClick={onClose} aria-label="Close dialog">
                  <X size={18} />
                </button>
              )}
            </div>
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

Dialog.Body = function DialogBody({ children, className = '' }) {
  return <div className={`dialog__body ${className}`}>{children}</div>
}
Dialog.Footer = function DialogFooter({ children, className = '' }) {
  return <div className={`dialog__footer ${className}`}>{children}</div>
}
