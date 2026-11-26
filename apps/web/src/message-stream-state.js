// Keep progressive-render state outside React component instances. Agent panels
// can unmount when the user switches workspaces; stable message IDs let a
// remounted panel resume a live message or render an already completed one
// without replaying its history.
const MAX_RETAINED_MESSAGES = 500
const revealStates = new Map()

export function getMessageRevealState(messageId, sourceText) {
  if (!messageId) return null
  const key = String(messageId)
  const state = revealStates.get(key)
  if (!state) return null

  // Refresh insertion order so active/recent messages are not evicted first.
  revealStates.delete(key)
  revealStates.set(key, state)

  const text = String(sourceText ?? '')
  if (text === state.sourceText) {
    return {
      visibleText: state.completed ? text : state.visibleText,
      completed: state.completed,
    }
  }

  // A message can continue receiving deltas while it is being revealed. Keep
  // the user's current reveal offset rather than restarting from the prefix.
  if (text.startsWith(state.sourceText)) {
    return {
      visibleText: state.completed ? state.sourceText : state.visibleText,
      completed: false,
    }
  }

  return null
}

export function saveMessageRevealState(messageId, state) {
  if (!messageId || !state || typeof state !== 'object') return
  const key = String(messageId)
  const sourceText = String(state.sourceText ?? '')
  const visibleText = String(state.visibleText ?? '')
  revealStates.delete(key)
  revealStates.set(key, {
    sourceText,
    visibleText: sourceText.startsWith(visibleText) ? visibleText : sourceText,
    completed: state.completed === true || visibleText.length >= sourceText.length,
  })
  while (revealStates.size > MAX_RETAINED_MESSAGES) {
    revealStates.delete(revealStates.keys().next().value)
  }
}

// Exposed for deterministic unit tests; not used by the application runtime.
export function clearMessageRevealStates() {
  revealStates.clear()
}
