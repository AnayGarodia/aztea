import { AnimatePresence, motion } from 'motion/react'
import { CheckCircle, XCircle, Info, AlertTriangle } from 'lucide-react'
import './Toast.css'

const ICONS = {
  success: <CheckCircle size={16} />,
  error:   <XCircle size={16} />,
  info:    <Info size={16} />,
  warn:    <AlertTriangle size={16} />,
}

export default function Toast({ toast }) {
  return (
    <AnimatePresence>
      {toast && (
        <motion.div
          key={toast.id}
          className={`toast toast--${toast.type ?? 'info'}`}
          initial={{ opacity: 0, y: -16 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -16 }}
          transition={{ duration: 0.2 }}
        >
          {ICONS[toast.type ?? 'info']}
          {toast.msg}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
