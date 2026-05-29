// OWNS: pure mapping from the /wallets/connect/status response shape
//       to a discriminated ConnectState union the WalletPage can render
//       against.
// NOT OWNS: rendering. The UI is owned by WalletPage; this module is a
//       header-style pure function suite so the state machine is testable
//       in isolation.
// DECISIONS:
//   * 5 connect states (not_started | kyc_in_progress | kyc_rejected |
//     bank_pending_microdeposit | bank_connected_ready) match the
//     buyer-facing UI brief from the 2026-05-26 wave-1 plan.
//   * The function is deliberately defensive — Stripe Connect's
//     `Account.requirements` shape changes occasionally; treat unknown
//     payloads as `not_started` rather than crash the wallet page.

/** @typedef {(
 *   'not_started' |
 *   'kyc_in_progress' |
 *   'kyc_rejected' |
 *   'bank_pending_microdeposit' |
 *   'bank_connected_ready'
 * )} ConnectState */

/** @typedef {(
 *   'none' |
 *   'initiated' |
 *   'in_transit' |
 *   'failed_retry' |
 *   'completed'
 * )} PayoutState */

/**
 * Pure: map a /wallets/connect/status payload to a ConnectState.
 *
 * The status payload (current backend shape) carries `connected`,
 * `charges_enabled`, and `account_id`. The 2026-05-26 wave-1 backend
 * extension also surfaces `kyc_status`, `bank_status`, and
 * `requirements_currently_due`; this helper degrades cleanly when the
 * extended fields are absent (older deploys, OSS mode, hosted outage).
 *
 * @param {object | null | undefined} status
 * @returns {ConnectState}
 */
export function deriveConnectState(status) {
  if (!status || typeof status !== 'object') return 'not_started'
  if (status.unavailable) return 'not_started'

  const kyc = String(status.kyc_status || '').toLowerCase()
  const bank = String(status.bank_status || '').toLowerCase()
  if (kyc === 'rejected') return 'kyc_rejected'

  if (status.connected !== true) {
    // Bank not linked at all yet.
    return kyc === 'pending' ? 'kyc_in_progress' : 'not_started'
  }
  if (status.charges_enabled !== true) {
    // Onboarding started but Stripe hasn't enabled the account yet —
    // KYC, identity, or one more verification step is still outstanding.
    if (bank === 'pending_microdeposit') return 'bank_pending_microdeposit'
    return 'kyc_in_progress'
  }
  // charges_enabled + connected — ready to receive payouts.
  return 'bank_connected_ready'
}

/**
 * Pure: map a withdrawal row's `status` field to a discriminated
 * PayoutState union. Backend uses 'pending' | 'initiated' |
 * 'in_transit' | 'failed' | 'completed'. We collapse 'pending' into
 * 'initiated' since the buyer-facing distinction is meaningless.
 *
 * @param {string | null | undefined} raw
 * @returns {PayoutState}
 */
export function derivePayoutState(raw) {
  const s = String(raw || '').toLowerCase()
  if (s === 'completed' || s === 'paid' || s === 'succeeded') return 'completed'
  if (s === 'in_transit') return 'in_transit'
  if (s === 'failed' || s === 'returned') return 'failed_retry'
  if (s === 'pending' || s === 'initiated' || s === 'processing') return 'initiated'
  return 'none'
}

/**
 * Pure: human-readable label for each ConnectState. Kept here next to
 * the union so adding a state forces both the label and any tests to
 * stay in lockstep.
 */
export const CONNECT_STATE_LABELS = {
  not_started: 'Not started',
  kyc_in_progress: 'Verification in progress',
  kyc_rejected: 'Verification failed',
  bank_pending_microdeposit: 'Bank verification pending',
  bank_connected_ready: 'Ready to withdraw',
}

export const PAYOUT_STATE_LABELS = {
  none: '—',
  initiated: 'Initiated',
  in_transit: 'In transit',
  failed_retry: 'Failed (retry available)',
  completed: 'Completed',
}
