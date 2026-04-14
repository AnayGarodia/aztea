import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import EmptyState from '../ui/EmptyState'
import { getJobMessages } from '../api'
import { useMarket } from '../context/MarketContext'

export default function JobDetailPage() {
  const { id } = useParams()
  const { jobs, apiKey } = useMarket()
  const [messages, setMessages] = useState([])
  const [loadingMessages, setLoadingMessages] = useState(false)

  const job = useMemo(() => jobs.find((item) => item.job_id === id), [jobs, id])

  useEffect(() => {
    let active = true
    if (!id || !apiKey) return () => {}
    setLoadingMessages(true)
    getJobMessages(apiKey, id)
      .then((res) => {
        if (!active) return
        setMessages(Array.isArray(res?.messages) ? res.messages : [])
      })
      .catch(() => {
        if (!active) return
        setMessages([])
      })
      .finally(() => {
        if (active) setLoadingMessages(false)
      })
    return () => {
      active = false
    }
  }, [apiKey, id])

  if (!job) {
    return (
      <main style={{ padding: 24, display: 'grid', gap: 16 }}>
        <Topbar crumbs={[{ to: '/jobs', label: 'Jobs' }, { label: 'Job detail' }]} />
        <EmptyState title="Job not found" sub="This job may no longer be visible to your key." />
      </main>
    )
  }

  return (
    <main style={{ padding: 24, display: 'grid', gap: 16 }}>
      <Topbar crumbs={[{ to: '/jobs', label: 'Jobs' }, { label: job.job_id }]} />
      <Card>
        <Card.Header>
          <strong>Job summary</strong>
        </Card.Header>
        <Card.Body>
          <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(job, null, 2)}</pre>
        </Card.Body>
      </Card>
      <Card>
        <Card.Header>
          <strong>Messages</strong>
        </Card.Header>
        <Card.Body>
          {loadingMessages ? (
            <p style={{ color: 'var(--ink-mute)' }}>Loading messages…</p>
          ) : messages.length === 0 ? (
            <p style={{ color: 'var(--ink-mute)' }}>No messages.</p>
          ) : (
            <div style={{ display: 'grid', gap: 10 }}>
              {messages.map((msg) => (
                <div key={msg.message_id} style={{ border: '1px solid var(--line)', borderRadius: 10, padding: 10 }}>
                  <p style={{ margin: '0 0 6px 0' }}><strong>{msg.type}</strong> · {msg.from_id}</p>
                  <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(msg.payload, null, 2)}</pre>
                </div>
              ))}
            </div>
          )}
        </Card.Body>
      </Card>
    </main>
  )
}
