import test from 'node:test'
import assert from 'node:assert/strict'
import { auditDetailEntries } from './audit-details.js'

test('audit details preserve ordinary names and hyphenated status values as whole values', () => {
  const entries = auditDetailEntries({ beneficiary: 'John Mitchell', sca: 'demo-confirmed' })
  assert.deepEqual(entries.map(({ key, value, wrap }) => ({ key, value, wrap })), [
    { key: 'beneficiary', value: 'John Mitchell', wrap: false },
    { key: 'sca', value: 'demo-confirmed', wrap: false },
  ])
})

test('audit details mark long identifiers for controlled wrapping', () => {
  const entries = auditDetailEntries({
    preview_id: 'b9ded74d-b39d-467b-a0d5-5fc6c80a8263',
    payload_digest: '311f027897c73f5c66afc30eceaa67ad3fb6e43fe4363e1194ade83fd8d3ee984',
    amount_minor: 5000,
  })
  assert.equal(entries[0].wrap, true)
  assert.equal(entries[1].wrap, true)
  assert.equal(entries[2].wrap, false)
})

test('nested audit details are flattened with stable dotted paths', () => {
  const entries = auditDetailEntries({
    reconciliation: { status: 'MATCHED', transaction_id: 'txn-1234567890' },
  })
  assert.deepEqual(entries.map(({ key }) => key), ['reconciliation.status', 'reconciliation.transaction_id'])
})

test('audit details accept JSON-string payloads from API serializers', () => {
  const payload = JSON.stringify({ beneficiary: 'John Mitchell', sca: 'demo-confirmed', transaction_id: 'txn-12345678' })
  const entries = auditDetailEntries(payload)
  assert.deepEqual(entries.map(({ key, value, wrap }) => ({ key, value, wrap })), [
    { key: 'beneficiary', value: 'John Mitchell', wrap: false },
    { key: 'sca', value: 'demo-confirmed', wrap: false },
    { key: 'transaction_id', value: 'txn-12345678', wrap: true },
  ])
})

test('non-JSON detail strings remain visible without being interpreted as JSON', () => {
  const entries = auditDetailEntries('connector returned plain text')
  assert.equal(entries.length, 1)
  assert.equal(entries[0].label, 'details')
  assert.equal(entries[0].value, 'connector returned plain text')
})
