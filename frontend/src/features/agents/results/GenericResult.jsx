import './ResultRenderer.css'

export default function GenericResult({ result }) {
  return (
    <div className="result">
      <p className="result-section__label" style={{ marginBottom: 8 }}>Result</p>
      <pre className="result-json">{JSON.stringify(result, null, 2)}</pre>
    </div>
  )
}
