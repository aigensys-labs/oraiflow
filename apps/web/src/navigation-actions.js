/**
 * One-shot browser navigation action receipts.
 *
 * CopilotKit can re-register frontend tools when their React dependency list
 * changes. A route-changing action must therefore be idempotent independently
 * of hook registration: the same runtime action_id can navigate at most once.
 */
export async function executeNavigationOnce(receipts, actionId, action) {
  if (!(receipts instanceof Map)) throw new TypeError('Navigation receipts must be a Map')
  const id = String(actionId || '').trim()
  if (!/^nav-[A-Za-z0-9_-]{8,64}$/.test(id)) {
    return { status: 'rejected', reason: 'A valid runtime navigation action identifier is required.' }
  }

  const previous = receipts.get(id)
  if (previous) {
    if (previous.promise) return previous.promise
    if (previous.result) return previous.result
  }

  const entry = { promise: null, result: null }
  const promise = Promise.resolve().then(action).then((result) => {
    entry.result = result
    entry.promise = null
    // Keep a bounded in-session receipt set. Recent actions remain protected
    // from replay after route changes or CopilotKit tool re-registration.
    while (receipts.size > 128) {
      const oldestKey = receipts.keys().next().value
      if (oldestKey === id) break
      const oldest = receipts.get(oldestKey)
      if (oldest?.promise) break
      receipts.delete(oldestKey)
    }
    return result
  }).catch((error) => {
    // A thrown action has no verified side effect; allow the same id to retry.
    receipts.delete(id)
    throw error
  })
  entry.promise = promise
  receipts.set(id, entry)
  return promise
}

/** A route revision makes manual customer navigation authoritative. */
export function advanceManualNavigationEpoch(currentEpoch) {
  const current = Number.isSafeInteger(currentEpoch) && currentEpoch >= 0 ? currentEpoch : 0
  return current + 1
}

/** Reject stale agent actions after the customer has manually changed tabs. */
export function isNavigationEpochCurrent(actionEpoch, currentEpoch) {
  return Number.isSafeInteger(actionEpoch)
    && Number.isSafeInteger(currentEpoch)
    && actionEpoch >= 0
    && currentEpoch >= 0
    && actionEpoch === currentEpoch
}
