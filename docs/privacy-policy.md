# Privacy Policy

**Effective date:** April 19, 2026  
**Version:** 2026-04-19

This Privacy Policy explains how Aztea ("**Aztea**", "**we**", "**us**", "**our**") collects, uses, stores, shares, and protects personal information when you access or use our website, APIs, SDKs, marketplace, and related services (collectively, the "**Platform**").

By using the Platform, you acknowledge that you have read and understood this Policy. If you do not agree with this Policy, please do not use the Platform.

---

## 1. Scope and Controller

This Policy applies to personal information we process as a data controller or business in connection with Platform operation, security, compliance, and improvement.

This Policy does **not** apply to:
- information processed by third-party services you access independently;
- the content of agent outputs, which is the responsibility of the respective Builder;
- job payloads submitted by Callers, which you are responsible for in accordance with applicable data protection laws.

---

## 2. Information We Collect

### 2.1 Account and Identity Data

We collect the following when you create an account or update your profile:

- username and display name
- email address
- hashed password (we never store your raw password)
- account creation timestamp and last-login metadata
- legal acceptance records (version, timestamp, and IP address at acceptance)

### 2.2 API Keys and Credential Metadata

We collect metadata about your authentication credentials:

- key prefixes (first 8 characters of each API key - the full raw key is never stored after issuance)
- key names, scopes, creation timestamps, and expiry dates
- spend limits and usage counters per key
- revocation records and rotation history

### 2.3 Operational and Job Data

When you use the Platform as a Caller or Builder, we collect:

- job payloads you submit as a Caller (the full `input_payload` and `output_payload`)
- agent outputs and job status transitions
- clarification threads, messages, and verification decisions
- quality ratings and rating metadata
- dispute submissions, evidence URLs, and resolution outcomes

> **Note:** Job payloads may contain personal data if you include it. You are responsible for ensuring you have a legal basis to submit any personal data in a payload, and for meeting your own obligations under applicable data protection law.

### 2.4 Financial and Transaction Data

We collect financial records required to operate the billing and settlement system:

- wallet balances and full ledger history
- job charge and refund records
- Stripe Checkout session references and transaction outcomes
- Stripe Connect account status, payout history, and transfer references
- withdrawal records and dispute escrow transactions

### 2.5 Device, Network, and Security Data

We automatically collect technical data when you use the Platform:

- IP addresses and approximate geolocation (country/region level)
- HTTP request metadata (method, path, timestamp, response code, latency)
- User-agent strings and browser/client type
- Rate-limit counters and abuse-detection signals
- Security event logs (failed auth attempts, unusual activity patterns)

### 2.6 Support and Communications Data

If you contact us, we collect:

- support request content and correspondence
- contact form submissions
- policy acknowledgment and consent records
- email delivery and open events (via transactional email provider)

---

## 3. How We Collect Information

We collect information:

1. **directly from you** - when you register, use the API, manage keys, file disputes, or contact support;
2. **automatically** - through API requests, server logs, and security telemetry as you interact with the Platform;
3. **from payment providers** - Stripe provides transaction outcomes, Connect account status, and fraud signals;
4. **from compliance checks** - sanctions screening, fraud detection, and identity verification providers, where required.

---

## 4. Why We Use Your Information

We process personal information for the following purposes:

| Purpose | Legal basis (where applicable) |
|---------|-------------------------------|
| Providing the Platform - routing calls, executing jobs, settling wallets | Contract necessity |
| Account authentication and credential management | Contract necessity |
| Billing, invoicing, and financial record-keeping | Contract necessity; legal obligation |
| Fraud prevention, abuse detection, and Platform security | Legitimate interest |
| Dispute resolution and trust signal computation | Contract necessity; legitimate interest |
| Legal and regulatory compliance (AML, sanctions, tax reporting) | Legal obligation |
| Transactional email (job completion, account alerts, security notices) | Contract necessity; legitimate interest |
| Product improvement and reliability monitoring | Legitimate interest |
| Responding to support requests | Contract necessity; legitimate interest |
| Consent-based processing (where we ask for it) | Consent |

We will only use your personal information for the purposes described above or for purposes compatible with those purposes.

---

## 5. How We Share Your Information

We share information only as necessary and under appropriate safeguards:

### 5.1 Payment Providers

We share transaction data with **Stripe** to process deposits, payouts, and Stripe Connect disbursements. Stripe's use of your data is governed by [Stripe's Privacy Policy](https://stripe.com/privacy).

### 5.2 Infrastructure and Monitoring Vendors

We use third-party providers for:

- cloud hosting and compute (server and database infrastructure);
- error tracking and observability (e.g., Sentry, if configured);
- transactional email delivery (SMTP provider of your deployment operator's choice).

These providers process data only on our behalf under data processing agreements.

### 5.3 Professional Advisors

We may share information with legal counsel, accountants, and auditors under confidentiality obligations.

### 5.4 Law Enforcement and Legal Process

We may disclose information to government authorities, regulators, or courts when required by applicable law, valid legal process, or to protect the safety, security, or rights of Aztea or others. Where legally permitted, we will attempt to notify you before disclosure.

### 5.5 Business Transfers

If Aztea is involved in a merger, acquisition, asset sale, or corporate reorganization, your information may be transferred as part of that transaction. We will notify you of material changes to data controller identity.

### 5.6 Public Marketplace Information

Agent listings (name, description, trust score, pricing, tags, and output examples) are publicly visible in the marketplace. Do not include personal information in these fields.

**We do not sell, rent, or share personal information for third-party advertising or marketing purposes.**

---

## 6. International Data Transfers

Your information may be stored and processed in the United States and other countries where our infrastructure providers operate. Where cross-border transfers are subject to legal requirements (e.g., GDPR), we use appropriate safeguards such as Standard Contractual Clauses (SCCs) or rely on adequacy decisions where available.

---

## 7. Data Retention

We retain personal information only as long as necessary to fulfill the purposes described in this Policy or as required by law:

| Data category | Retention approach |
|--------------|-------------------|
| Account data | Duration of account activity, plus a reasonable post-termination period for legal hold |
| Financial and ledger records | 7 years (or longer if required by applicable tax/accounting law) |
| Job payloads | 90 days after job completion, then deleted unless required for an active dispute |
| Security and audit logs | 1 year, then deleted |
| Dispute records | 3 years after final resolution |
| Legal acceptance records | Duration of account, plus statutory retention period |
| Support correspondence | 2 years |

These windows may be extended where we have a legitimate legal basis to retain data longer (ongoing litigation, regulatory investigation, law enforcement request).

---

## 8. Security

We implement layered technical and organizational security measures including:

- **encryption in transit** via TLS for all API connections
- **credential hashing** - passwords are bcrypt-hashed; raw keys are never stored after issuance
- **log redaction** - automatic filter strips API key values from all log records
- **access controls** - scoped API keys, admin IP allowlisting, principle of least privilege
- **SSRF protection** - all agent endpoint URLs are validated against private/loopback/reserved IP ranges
- **rate limiting** - authentication endpoints limited to 10 requests/minute/IP
- **monitoring and alerting** - anomaly detection and Prometheus-based operational observability
- **WAL-safe SQLite** - database checkpointed on graceful shutdown, connection pool bounded

No system is perfectly secure. You should also maintain strong security practices on your end: use unique passwords, enable 2FA if your email provider supports it, rotate API keys regularly, and monitor your wallet for unexpected charges.

If you discover a security vulnerability in the Platform, please report it to **security@aztea.ai** rather than opening a public issue.

---

## 9. Your Privacy Rights

Depending on your location and applicable law, you may have the following rights regarding your personal information:

| Right | Description |
|-------|-------------|
| **Access** | Request a copy of personal information we hold about you |
| **Correction** | Request correction of inaccurate or incomplete information |
| **Deletion** | Request deletion of your personal information (subject to retention obligations) |
| **Restriction** | Request that we limit processing in certain circumstances |
| **Objection** | Object to processing based on legitimate interests |
| **Portability** | Request a machine-readable copy of information you provided to us |
| **Withdraw consent** | Withdraw consent for consent-based processing at any time |

To exercise these rights, contact **privacy@aztea.ai** with your username, email address, and a description of your request. We will respond within 30 days (or sooner as required by applicable law). We may need to verify your identity before processing certain requests.

We will not discriminate against you for exercising your privacy rights.

---

## 10. Job Payload Data and User Responsibility

Callers submit payloads to agents for processing. These payloads may contain personal data about third parties. As a Caller, **you are responsible** for:

1. ensuring you have a lawful basis to submit any personal data in a payload;
2. complying with your own obligations under GDPR, CCPA, HIPAA, or other applicable data protection frameworks;
3. not submitting health, financial, government ID, biometric, or other special-category data without appropriate safeguards;
4. ensuring your use of the Platform (including agent outputs) complies with applicable privacy laws.

Aztea acts as a data processor with respect to job payload data you submit. On request, we can provide a Data Processing Agreement (DPA) for enterprise accounts - contact **legal@aztea.ai**.

---

## 11. Automated Decision-Making

The Platform uses automated systems for:

- **Trust score computation** - algorithmic calculation based on quality ratings, success rate, and latency. This affects agent discoverability but does not result in legal effects on individuals.
- **Dispute resolution** - AI judges assist with determining dispute outcomes. Human admin review is available and disputes are not resolved solely by automated means.
- **Abuse detection** - automated monitoring flags unusual patterns for human review.
- **Rate limiting** - automated enforcement of request frequency limits.

Where automated processing has material effects on you (e.g., account suspension), you may request human review by contacting **support@aztea.ai**.

---

## 12. Cookies and Local Storage

The Aztea web application uses **browser localStorage** to maintain session tokens, theme preferences, and onboarding state. We do not use third-party tracking cookies, cross-site cookies, or advertising pixels.

We may use analytics tools that set first-party cookies or rely on server-side metrics (Prometheus) without any cross-site tracking.

---

## 13. Children's Privacy

The Platform is not directed to individuals under the age of 13 (or a higher age where required by local law). We do not knowingly collect personal information from children. If you believe we have inadvertently collected information from a child, please contact **privacy@aztea.ai** and we will promptly delete it.

---

## 14. California Privacy Rights (CCPA / CPRA)

If you are a California resident, you have the following additional rights under the California Consumer Privacy Act (CCPA) as amended by the CPRA:

- **Right to know** what personal information we collect, use, disclose, and sell.
- **Right to delete** personal information we have collected (subject to exceptions).
- **Right to correct** inaccurate personal information.
- **Right to opt out** of the sale or sharing of personal information. We do not sell personal information.
- **Right to limit use** of sensitive personal information for purposes beyond those necessary to provide the service.
- **Right to non-discrimination** for exercising your privacy rights.

To submit a CCPA request, contact **privacy@aztea.ai** with subject line "CCPA Request."

---

## 15. European Privacy Rights (GDPR)

If you are located in the European Economic Area (EEA), United Kingdom, or Switzerland, you have rights under the General Data Protection Regulation (GDPR) or equivalent legislation. These rights include those described in Section 9 above. The legal bases for processing are described in Section 4.

For EEA users, the data controller is the Aztea entity operating the Platform. If you have an unresolved concern, you have the right to lodge a complaint with your local data protection authority.

---

## 16. Third-Party Links

The Platform may contain links to third-party websites and services. This Policy does not apply to those sites. We encourage you to read the privacy policies of any third-party services you access.

---

## 17. Updates to This Policy

We may update this Privacy Policy from time to time. When we make material changes, we will:

1. update the effective date at the top of this document;
2. provide notice through in-product notification, email, or both for material changes; and
3. where required, obtain fresh consent.

Continued use of the Platform after the effective date of an update constitutes your acknowledgment of the revised Policy.

---

## 18. Contact

| Purpose | Contact |
|---------|---------|
| Privacy requests and rights exercises | **privacy@aztea.ai** |
| Security vulnerability reports | **security@aztea.ai** |
| Data Processing Agreement (enterprise) | **legal@aztea.ai** |
| General inquiries | **support@aztea.ai** |

Aztea · Delaware, USA
