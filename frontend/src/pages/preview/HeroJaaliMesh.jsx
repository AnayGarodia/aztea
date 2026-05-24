import AzteaMark from '../../brand/AzteaMark'
import './HeroJaaliMesh.css'

export default function HeroJaaliMesh() {
  return (
    <div className="hjm">
      <div className="hjm__gradient hjm__gradient--terracotta" />
      <div className="hjm__gradient hjm__gradient--copper" />
      <div className="hjm__gradient hjm__gradient--accent" />
      <div className="hjm__mark hjm__mark--primary">
        <AzteaMark size={720} animate={true} />
      </div>
      <div className="hjm__mark hjm__mark--secondary">
        <AzteaMark size={320} animate={true} />
      </div>
    </div>
  )
}
