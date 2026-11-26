import test from 'node:test'
import assert from 'node:assert/strict'
import { executeNavigationOnce, advanceManualNavigationEpoch, isNavigationEpochCurrent } from './navigation-actions.js'

test('the same navigation action id applies the route exactly once', async () => {
  const receipts = new Map()
  let navigations = 0
  const action = async () => {
    navigations += 1
    return { status: 'opened', workspace: 'Contacts', action_id: 'nav-1234567890abcdef' }
  }
  const first = await executeNavigationOnce(receipts, 'nav-1234567890abcdef', action)
  const replay = await executeNavigationOnce(receipts, 'nav-1234567890abcdef', action)
  assert.equal(navigations, 1)
  assert.deepEqual(replay, first)
})

test('concurrent retries of an in-flight navigation share the same action', async () => {
  const receipts = new Map()
  let navigations = 0
  let resolveAction
  const action = () => {
    navigations += 1
    return new Promise((resolve) => { resolveAction = resolve })
  }
  const first = executeNavigationOnce(receipts, 'nav-1234567890abcdef', action)
  const second = executeNavigationOnce(receipts, 'nav-1234567890abcdef', action)
  await Promise.resolve()
  assert.equal(navigations, 1)
  resolveAction({ status: 'opened', workspace: 'Contacts', action_id: 'nav-1234567890abcdef' })
  assert.deepEqual(await first, await second)
})

test('a failed action does not get cached as a successful navigation', async () => {
  const receipts = new Map()
  let attempts = 0
  await assert.rejects(() => executeNavigationOnce(receipts, 'nav-1234567890abcdef', async () => {
    attempts += 1
    throw new Error('temporary failure')
  }))
  const retry = await executeNavigationOnce(receipts, 'nav-1234567890abcdef', async () => {
    attempts += 1
    return { status: 'opened', workspace: 'Contacts', action_id: 'nav-1234567890abcdef' }
  })
  assert.equal(attempts, 2)
  assert.equal(retry.status, 'opened')
})

test('missing or malformed runtime action IDs are rejected without navigation', async () => {
  const receipts = new Map()
  let navigations = 0
  const result = await executeNavigationOnce(receipts, 'not-an-action-id', async () => {
    navigations += 1
    return { status: 'opened', workspace: 'Contacts' }
  })
  assert.equal(result.status, 'rejected')
  assert.equal(navigations, 0)
})


test('a manual tab change invalidates an in-flight agent navigation captured on an earlier route revision', () => {
  const initialEpoch = 0
  const afterManualClick = advanceManualNavigationEpoch(initialEpoch)
  assert.equal(afterManualClick, 1)
  assert.equal(isNavigationEpochCurrent(initialEpoch, afterManualClick), false)
  assert.equal(isNavigationEpochCurrent(afterManualClick, afterManualClick), true)
})
