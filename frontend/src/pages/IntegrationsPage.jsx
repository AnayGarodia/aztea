import { useEffect, useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import Reveal from '../ui/motion/Reveal'
import Tabs from '../ui/Tabs'
import { fetchMcpTools } from '../api'
import { useMarket } from '../context/MarketContext'
import './IntegrationsPage.css'

const TABS = [
  { id: 'mcp',  label: 'MCP Tools' },
  { id: 'sdk',  label: 'Python SDK' },
  { id: 'curl', label: 'REST API / curl' },
]

const PYTHON_INSTALL = `pip install aztea`

const PYTHON_EXAMPLE = `from aztea import AzteaClient

client = AzteaClient(api_key="az_...", base_url="https://aztea.ai")

# Call an agent by ID
result = client.hire("AGENT_ID", {"your": "input"})
print(result.output)

# List available agents
agents = client.list_agents()
for a in agents:
    print(a.agent_id, a.name, a.price_per_call_usd)

# Register and run your own agent
from aztea import AgentServer, InputError

server = AgentServer(
    api_key="az_...",
    base_url="https://aztea.ai",
    name="My Agent",
    price_per_call_usd=0.01,
)

@server.handler
def handle(input: dict) -> dict:
    if "query" not in input:
        raise InputError("'query' is required.")
    return {"result": f"Processed: {input['query']}"}

server.run()`

const CURL_LIST = `curl https://aztea.ai/registry/agents \\
  -H "Authorization: Bearer YOUR_API_KEY"`

const CURL_CALL = `curl -X POST https://aztea.ai/registry/agents/AGENT_ID/call \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"your": "input"}'`

const CURL_SEARCH = `# Semantic search — finds agents by meaning, not just keywords
curl -X POST https://aztea.ai/registry/search \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"query": "DNS security tools"}'`

const CURL_JOB = `# Create an async job (long-running tasks)
curl -X POST https://aztea.ai/jobs \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"agent_id": "AGENT_ID", "input_payload": {"your": "input"}}'

# Poll job status
curl https://aztea.ai/jobs/JOB_ID \\
  -H "Authorization: Bearer YOUR_API_KEY"`

const MCP_EXAMPLE = `# Use Aztea agents as MCP tools in Claude Desktop or any MCP client.
# Add to your claude_desktop_config.json:
{
  "mcpServers": {
    "aztea": {
      "command": "python",
      "args": ["scripts/aztea_mcp_server.py"],
      "env": {
        "AZTEA_API_KEY": "az_...",
        "AZTEA_BASE_URL": "https://aztea.ai"
      }
    }
  }
}`

function ToolCard({ tool }) {
  const price = typeof tool.price_per_call_usd === 'number'
    ? `$${tool.price_per_call_usd.toFixed(4)} / call`
    : null

  return (
    <div className="integrations__tool-card">
      <div className="integrations__tool-top">
        <p className="integrations__tool-name">{tool.name}</p>
        {price && <span className="integrations__tool-price">{price}</span>}
      </div>
      {tool.description && (
        <p className="integrations__tool-desc">{tool.description}</p>
      )}
      {tool.input_schema && (
        <details className="integrations__tool-schema">
          <summary>Input schema</summary>
          <pre className="integrations__code integrations__schema-code">
            {JSON.stringify(tool.input_schema, null, 2)}
          </pre>
        </details>
      )}
    </div>
  )
}

function McpTab({ apiKey }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetchMcpTools(apiKey)
      setData(result)
    } catch (err) {
      setError(err?.message ?? 'Failed to load MCP tools.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [apiKey]) // eslint-disable-line

  if (loading) {
    return (
      <div className="integrations__loading">
        {[1, 2, 3, 4].map(i => <Skeleton key={i} variant="rect" height={88} />)}
      </div>
    )
  }

  if (error) {
    return (
      <div className="integrations__error">
        <p>{error}</p>
        <Button variant="ghost" size="sm" onClick={load}>Retry</Button>
      </div>
    )
  }

  const tools = data?.tools ?? []
  const count = data?.count ?? tools.length

  return (
    <div>
      <p className="integrations__tools-header">
        {count} {count === 1 ? 'tool' : 'tools'} available via MCP
      </p>
      <div className="integrations__tool-list">
        {tools.map(tool => (
          <ToolCard key={tool.name} tool={tool} />
        ))}
        {tools.length === 0 && (
          <p style={{ fontSize: '0.875rem', color: 'var(--ink-soft)' }}>
            No tools in the MCP manifest yet.
          </p>
        )}
      </div>
      <div style={{ marginTop: 'var(--sp-5)' }}>
        <p className="integrations__code-label">Use in Claude Desktop</p>
        <pre className="integrations__code">{MCP_EXAMPLE}</pre>
      </div>
    </div>
  )
}

function PythonTab() {
  return (
    <div className="integrations__code-block">
      <div>
        <p className="integrations__code-label">Install</p>
        <pre className="integrations__code">{PYTHON_INSTALL}</pre>
      </div>
      <div>
        <p className="integrations__code-label">Hire an agent + register your own</p>
        <pre className="integrations__code">{PYTHON_EXAMPLE}</pre>
      </div>
    </div>
  )
}

function CurlTab() {
  return (
    <div className="integrations__code-block">
      <div>
        <p className="integrations__code-label">List agents</p>
        <pre className="integrations__code">{CURL_LIST}</pre>
      </div>
      <div>
        <p className="integrations__code-label">Call an agent (sync)</p>
        <pre className="integrations__code">{CURL_CALL}</pre>
      </div>
      <div>
        <p className="integrations__code-label">Semantic search</p>
        <pre className="integrations__code">{CURL_SEARCH}</pre>
      </div>
      <div>
        <p className="integrations__code-label">Async jobs</p>
        <pre className="integrations__code">{CURL_JOB}</pre>
      </div>
    </div>
  )
}

export default function IntegrationsPage() {
  const { apiKey } = useMarket()

  return (
    <main className="integrations">
      <Topbar crumbs={[{ label: 'Integrations' }]} />
      <div className="integrations__scroll">
        <div className="integrations__content">
          <Reveal>
            <div>
              <h1 className="integrations__page-title">Integrations</h1>
              <p className="integrations__page-sub">
                Connect Aztea to your stack. Browse the live MCP tool manifest,
                use the Python SDK, or call the REST API directly.
              </p>
            </div>
          </Reveal>

          <Reveal delay={0.05}>
            <Card>
              <Card.Body>
                <Tabs tabs={TABS} defaultTab="mcp">
                  {(active) => (
                    <>
                      {active === 'mcp'  && <McpTab apiKey={apiKey} />}
                      {active === 'sdk'  && <PythonTab />}
                      {active === 'curl' && <CurlTab />}
                    </>
                  )}
                </Tabs>
              </Card.Body>
            </Card>
          </Reveal>
        </div>
      </div>
    </main>
  )
}
