/**
 * Decide whether a frontend transfer action may open another editable draft.
 *
 * The backend action/workflow IDs are stable for a replay of one customer
 * message. This browser-side guard is the final idempotency boundary: it makes
 * a repeated tool call harmless even if an AG-UI run is resumed after the
 * original modal has already been rendered.
 */
export function transferDraftReplayDecision({
  receipts,
  workflowId,
  actionId,
  lifecycle,
  visibleDraft,
  modalOpen,
}) {
  const receipt = receipts instanceof Map ? receipts.get(actionId) : null
  if (receipt) {
    if (receipt.workflow_id !== workflowId) return { kind: 'conflict' }
    if (receipt.action_id !== actionId) return { kind: 'conflict' }
    return receipt.status === 'open'
      ? { kind: 'already_open' }
      : { kind: 'already_terminal' }
  }

  const workflowReceipt = receipts instanceof Map
    ? [...receipts.values()].find((item) => item?.workflow_id === workflowId)
    : null
  if (workflowReceipt) {
    return workflowReceipt.action_id === actionId && workflowReceipt.status === 'open'
      ? { kind: 'already_open' }
      : { kind: 'conflict' }
  }

  if (lifecycle?.workflow_id === workflowId) {
    if (lifecycle.action_id !== actionId) return { kind: 'conflict' }
    return lifecycle.status === 'open'
      ? { kind: 'already_open' }
      : { kind: 'already_terminal' }
  }

  if (modalOpen) {
    return { kind: 'another_draft_open' }
  }

  if (visibleDraft?.workflow_id === workflowId) {
    return { kind: 'already_open' }
  }

  return { kind: 'open' }
}
