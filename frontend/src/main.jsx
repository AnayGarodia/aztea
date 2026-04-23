import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './theme/reset.css'
import './theme/fonts.css'
import './theme/tokens.css'
import './styles/globals.css'
import './styles/responsive.css'

// Sentry loads async so it never blocks the initial paint. If the DSN is
// missing (dev, self-host) the library is never fetched in the first place.
const SENTRY_DSN = import.meta.env.VITE_SENTRY_DSN
if (SENTRY_DSN) {
  import('@sentry/react')
    .then((Sentry) => {
      Sentry.init({
        dsn: SENTRY_DSN,
        environment: import.meta.env.VITE_ENVIRONMENT || 'production',
        tracesSampleRate: 0.1,
        replaysOnErrorSampleRate: 1.0,
      })
    })
    .catch(() => {})
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
