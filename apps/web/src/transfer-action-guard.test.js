import test from 'node:test'
import assert from 'node:assert/strict'
import { transferDraftReplayDecision } from './transfer-action-guard.js'

const base = {
  receipts: new Map(),
  workflowId: 'trf-request-1',
  actionId: 'act-draft-1',
  lifecycle: null,
  visibleDraft: null,
  modalOpen: false,
}

test('a replay of the same open action does not open a second draft', () => {
  const receipts = new Map([
    ['act-draft-1', { workflow_id: 'trf-request-1', action_id: 'act-draft-1', status: 'open' }],
  ])
  assert.deepEqual(transferDraftReplayDecision({ ...base, receipts }), { kind: 'already_open' })
})

test('a dismissed action cannot be reopened by a delayed replay', () => {
  const receipts = new Map([
    ['act-draft-1', { workflow_id: 'trf-request-1', action_id: 'act-draft-1', status: 'dismissed' }],
  ])
  assert.deepEqual(transferDraftReplayDecision({ ...base, receipts }), { kind: 'already_terminal' })
})

test('the current lifecycle context independently suppresses a replay', () => {
  assert.deepEqual(transferDraftReplayDecision({
    ...base,
    lifecycle: { workflow_id: 'trf-request-1', action_id: 'act-draft-1', status: 'open' },
  }), { kind: 'already_open' })
})

test('a second draft cannot replace a different draft while the modal is open', () => {
  assert.deepEqual(transferDraftReplayDecision({
    ...base,
    workflowId: 'trf-request-2',
    actionId: 'act-draft-2',
    modalOpen: true,
    visibleDraft: { workflow_id: 'trf-request-1' },
  }), { kind: 'another_draft_open' })
})

test('a genuinely new request can open a draft after the previous one is closed', () => {
  const receipts = new Map([
    ['act-draft-1', { workflow_id: 'trf-request-1', action_id: 'act-draft-1', status: 'dismissed' }],
  ])
  assert.deepEqual(transferDraftReplayDecision({
    ...base,
    receipts,
    workflowId: 'trf-request-2',
    actionId: 'act-draft-2',
  }), { kind: 'open' })
})

test('a workflow/action identity mismatch fails closed', () => {
  const receipts = new Map([
    ['act-draft-old', { workflow_id: 'trf-request-1', action_id: 'act-draft-old', status: 'open' }],
  ])
  assert.deepEqual(transferDraftReplayDecision({ ...base, receipts }), { kind: 'conflict' })
})
