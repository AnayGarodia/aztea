// errorCopy.js — turn an API Error (from api.js makeApiError) into warm,
// status-aware UI copy.
//
// Usage:
//   import { formatApiError } from "@/utils/errorCopy"
//   catch (err) {
//     setError(formatApiError(err, { action: "claim job" }).title)
//   }
//
// Why this file: every page used to write `err?.message ?? 'X failed.'` which
// throws away the context the server worked to send (status, code,
// request_id, retry_after). Centralising the branching lets us tune copy in
// one place and keeps the voice consistent across catch sites. The helper
// only fills the *fallback* — server-authored `message` always wins when
// present and non-generic.

import { API_ERROR_MESSAGE_BY_CODE } from "../api"

const GENERIC_MESSAGES = new Set([
  "",
  "not found",
  "request failed.",
  "internal server error.",
  "bad request",
  "forbidden",
  "unauthorized",
  "error",
  "failed",
])

function isGeneric(message) {
  if (!message) return true
  return GENERIC_MESSAGES.has(String(message).trim().toLowerCase())
}

function retryAfterFromError(err) {
  // Server-side make_error envelopes carry retry_after under details; the
  // make_error helper in core/error_codes.py uses the singular field name.
  const details = err?.body?.details
  if (details && typeof details === "object") {
    const candidate =
      details.retry_after_seconds ?? details.retry_after ?? details.retryAfter
    const parsed = Number.parseInt(candidate, 10)
    if (Number.isFinite(parsed) && parsed > 0) return parsed
  }
  return null
}

/**
 * Format an API error for inline display.
 *
 * @param {Error} err — the Error from api.js makeApiError (has .status,
 *   .code, .body, .requestId, .message).
 * @param {{ action?: string }} [options] — short verb phrase used in the
 *   "Could not <action>." fallback ("claim job", "file dispute"). Defaults
 *   to "complete the request".
 * @returns {{ title: string, hint: string|null, retryable: boolean }}
 */
export function formatApiError(err, options = {}) {
  const action = (options.action || "complete the request").trim()
  if (!err) {
    return { title: `Could not ${action}. Try again.`, hint: null, retryable: true }
  }

  const status = Number.isFinite(err.status) ? Number(err.status) : 0
  const code = typeof err.code === "string" && err.code.trim() ? err.code.trim() : null
  const serverMessage =
    typeof err.message === "string" && err.message.trim() ? err.message.trim() : null

  // 1. The server already authored a specific, non-generic message — trust it.
  //    This branch is the contract: money / dispute / security flows always
  //    win here because their messages are precise.
  if (serverMessage && !isGeneric(serverMessage)) {
    return {
      title: serverMessage,
      hint: hintForStatus(status, err),
      retryable: isRetryableStatus(status),
    }
  }

  // 2. The error code matches a known taxonomy entry — use the warm fallback.
  if (code && API_ERROR_MESSAGE_BY_CODE[code]) {
    return {
      title: API_ERROR_MESSAGE_BY_CODE[code],
      hint: hintForStatus(status, err),
      retryable: isRetryableStatus(status),
    }
  }

  // 3. Status-aware fallback. Branches by what the response actually means.
  const byStatus = titleForStatus(status, action, err)
  if (byStatus) {
    return {
      title: byStatus,
      hint: hintForStatus(status, err),
      retryable: isRetryableStatus(status),
    }
  }

  // 4. Last resort: use whatever the server sent, even if it's generic.
  return {
    title: serverMessage || `Could not ${action}. Try again.`,
    hint: null,
    retryable: isRetryableStatus(status),
  }
}

function titleForStatus(status, action, err) {
  switch (status) {
    case 401:
      return "Your session expired. Sign in again."
    case 402:
      return "Not enough wallet balance. Top up and retry."
    case 403:
      return `You don't have permission to ${action}.`
    case 404:
      return `Not found — double-check the id and retry.`
    case 409:
      // 409 messages are usually server-authored and specific; if we're here
      // it means the server sent a generic one. Surface what we know.
      return `That ${action.split(" ")[1] || "action"} conflicts with the current state.`
    case 422:
      return "Some inputs were invalid. Review the highlighted fields and try again."
    case 429: {
      const seconds = retryAfterFromError(err)
      if (seconds) return `Too many requests. Retry in ${seconds}s.`
      return "Too many requests. Wait a moment and try again."
    }
    case 500:
    case 502:
    case 503:
    case 504:
      return "The server hiccuped. We're looking into it — retry in a moment."
    default:
      return null
  }
}

function hintForStatus(status, err) {
  if (status >= 500 && err?.requestId) {
    return `request_id ${err.requestId}`
  }
  return null
}

function isRetryableStatus(status) {
  if (status === 429) return true
  if (status >= 500 && status < 600) return true
  return false
}
