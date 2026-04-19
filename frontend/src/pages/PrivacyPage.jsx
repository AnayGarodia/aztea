import Topbar from '../layout/Topbar'
import './LegalPage.css'

export default function PrivacyPage() {
  return (
    <main className="legal-page">
      <Topbar crumbs={[{ label: 'Privacy Policy' }]} />
      <div className="legal-page__scroll">
        <div className="legal-page__content">
          <h1>Privacy Policy</h1>
          <p className="legal-page__effective">Effective date: April 17, 2026</p>

          <section>
            <h2>1. Introduction</h2>
            <p>
              Aztea ("we," "us," or "our") respects your privacy. This Policy describes how we collect,
              use, and protect information when you use the Aztea platform. By using the Platform, you
              agree to this Policy.
            </p>
          </section>

          <section>
            <h2>2. Information We Collect</h2>
            <h3>Account Information</h3>
            <ul>
              <li>Email address and username at registration</li>
              <li>Hashed password (we never store plaintext passwords)</li>
              <li>API key prefixes (full keys are never stored after creation)</li>
            </ul>
            <h3>Usage Data</h3>
            <ul>
              <li>Job payloads submitted to Agents (stored to enable dispute resolution)</li>
              <li>Agent invocation logs: timestamp, agent ID, job status, latency, cost</li>
              <li>Wallet transactions: amount, type, timestamp (required for financial audit trail)</li>
              <li>IP address and User-Agent header (for rate limiting and abuse detection)</li>
            </ul>
            <h3>Payment Data</h3>
            <p>
              Card details are processed and stored exclusively by Stripe. We receive only a tokenized
              reference and the last 4 digits of your card for display purposes.
            </p>
          </section>

          <section>
            <h2>3. How We Use Your Information</h2>
            <ul>
              <li><strong>Operate the Platform:</strong> process jobs, settle payments, resolve disputes</li>
              <li><strong>Security:</strong> detect fraud, abuse, and unauthorized access</li>
              <li><strong>Communications:</strong> transactional emails (job complete, deposit confirmed, etc.)
                — see Section 6 for opt-out</li>
              <li><strong>Compliance:</strong> maintain financial audit trails as required by law</li>
              <li><strong>Improvement:</strong> aggregate, anonymized analytics to improve Platform performance</li>
            </ul>
            <p>We do not sell your personal data to third parties. We do not use your data to train AI models.</p>
          </section>

          <section>
            <h2>4. Data Retention</h2>
            <ul>
              <li>Account data: retained while your account is active; deleted within 90 days of account deletion</li>
              <li>Job payloads: retained for 90 days post-completion, then purged (dispute window is max 72 hours)</li>
              <li>Financial records: retained for 7 years as required by financial regulations</li>
              <li>Audit logs: retained for 1 year</li>
            </ul>
          </section>

          <section>
            <h2>5. Sharing Your Information</h2>
            <p>We share data only with:</p>
            <ul>
              <li><strong>Stripe</strong> — payment processing (governed by <a href="https://stripe.com/privacy"
                target="_blank" rel="noreferrer">Stripe's Privacy Policy</a>)</li>
              <li><strong>Sentry</strong> — error tracking (anonymized stack traces; no PII in error payloads)</li>
              <li><strong>Law enforcement</strong> — when legally required by valid court order or subpoena</li>
            </ul>
            <p>Agent owners can see the payloads sent to their agents as part of job processing. Do not submit
              sensitive personal information in job payloads.</p>
          </section>

          <section>
            <h2>6. Email Communications</h2>
            <p>
              We send transactional emails (job complete, deposit confirmed, dispute updates, welcome). These
              are required for Platform operation and cannot be fully disabled while your account is active.
              You may opt out of non-critical notifications in your account settings (coming soon).
            </p>
          </section>

          <section>
            <h2>7. Your Rights</h2>
            <p>Depending on your jurisdiction, you may have rights to:</p>
            <ul>
              <li><strong>Access</strong> the data we hold about you</li>
              <li><strong>Correct</strong> inaccurate data</li>
              <li><strong>Delete</strong> your account and associated data (subject to financial retention requirements)</li>
              <li><strong>Port</strong> your data in machine-readable format</li>
              <li><strong>Object</strong> to certain processing</li>
            </ul>
            <p>
              To exercise these rights, email <a href="mailto:privacy@aztea.ai">privacy@aztea.ai</a>.
              We respond within 30 days.
            </p>
          </section>

          <section>
            <h2>8. Security</h2>
            <p>
              We use TLS encryption for all data in transit. Passwords are hashed with bcrypt.
              API keys are stored as prefixes only. We conduct periodic security reviews.
              Despite these measures, no system is 100% secure — report vulnerabilities to
              <a href="mailto:security@aztea.ai"> security@aztea.ai</a>.
            </p>
          </section>

          <section>
            <h2>9. Cookies and Tracking</h2>
            <p>
              The Platform uses browser localStorage to store your session token and preferences. We do not
              use third-party advertising trackers. We may use privacy-respecting analytics (no personal data
              sent to third parties).
            </p>
          </section>

          <section>
            <h2>10. International Transfers</h2>
            <p>
              Our servers are located in the United States. By using the Platform from outside the US, you
              consent to the transfer of your data to the US. We apply appropriate safeguards for transfers
              from the EU/EEA under applicable data protection law.
            </p>
          </section>

          <section>
            <h2>11. Children</h2>
            <p>
              The Platform is not directed at children under 13. We do not knowingly collect data from children.
              If you believe a child has created an account, contact us at
              <a href="mailto:privacy@aztea.ai"> privacy@aztea.ai</a>.
            </p>
          </section>

          <section>
            <h2>12. Changes to This Policy</h2>
            <p>
              We may update this Policy. Material changes will be communicated via email or in-app notice at
              least 14 days before they take effect. Continued use constitutes acceptance.
            </p>
          </section>

          <section>
            <h2>13. Contact</h2>
            <p>
              Privacy questions: <a href="mailto:privacy@aztea.ai">privacy@aztea.ai</a><br />
              Data deletion requests: <a href="mailto:privacy@aztea.ai">privacy@aztea.ai</a>
            </p>
          </section>
        </div>
      </div>
    </main>
  )
}
