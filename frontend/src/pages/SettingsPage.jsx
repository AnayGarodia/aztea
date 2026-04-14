import { useEffect, useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Input from '../ui/Input'
import { createAuthKey, deleteAuthKey, fetchAuthKeys } from '../api'
import { useMarket } from '../context/MarketContext'

export default function SettingsPage() {
  const { apiKey, showToast } = useMarket()
  const [keys, setKeys] = useState([])
  const [name, setName] = useState('New key')
  const [loading, setLoading] = useState(false)

  const refreshKeys = async () => {
    try {
      const result = await fetchAuthKeys(apiKey)
      setKeys(Array.isArray(result?.keys) ? result.keys : [])
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to load API keys', 'error')
    }
  }

  useEffect(() => {
    refreshKeys()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiKey])

  const handleCreateKey = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      const created = await createAuthKey(apiKey, name, ['caller', 'worker'])
      showToast?.(`Created key ${created.key_prefix}`, 'success')
      await refreshKeys()
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to create key', 'error')
    } finally {
      setLoading(false)
    }
  }

  const handleRevoke = async (keyId) => {
    try {
      await deleteAuthKey(apiKey, keyId)
      showToast?.('Key revoked.', 'success')
      await refreshKeys()
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to revoke key', 'error')
    }
  }

  return (
    <main style={{ padding: 24, display: 'grid', gap: 16 }}>
      <Topbar crumbs={[{ label: 'Settings' }]} />
      <Card>
        <Card.Header>
          <strong>Create API key</strong>
        </Card.Header>
        <Card.Body>
          <form onSubmit={handleCreateKey} style={{ display: 'flex', alignItems: 'end', gap: 10 }}>
            <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} required />
            <Button type="submit" loading={loading}>Create key</Button>
          </form>
        </Card.Body>
      </Card>
      <Card>
        <Card.Header>
          <strong>Active keys</strong>
        </Card.Header>
        <Card.Body>
          {keys.length === 0 ? (
            <p style={{ color: 'var(--ink-mute)' }}>No keys found.</p>
          ) : (
            <div style={{ display: 'grid', gap: 8 }}>
              {keys.map((item) => (
                <div key={item.key_id} style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
                  <span>{item.name} · {item.key_prefix}</span>
                  <Button variant="danger" size="sm" onClick={() => handleRevoke(item.key_id)}>
                    Revoke
                  </Button>
                </div>
              ))}
            </div>
          )}
        </Card.Body>
      </Card>
    </main>
  )
}
