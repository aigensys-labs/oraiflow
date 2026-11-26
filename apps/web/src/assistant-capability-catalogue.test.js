import test from 'node:test'
import assert from 'node:assert/strict'
import { CONTRACT_REGISTRY } from './generated/contracts.js'

test('AI Assistant capability cards are registry-backed and uniquely identified', () => {
  const policy = CONTRACT_REGISTRY.application_policy
  const capabilities = policy.assistant_capabilities
  const workspaces = new Set(policy.navigation.pages)
  const intents = new Set(Object.keys(CONTRACT_REGISTRY.intents))
  const topics = new Set(policy.academy_topics.map((topic) => topic.topic_id))

  assert.ok(capabilities.length > 0)
  assert.equal(new Set(capabilities.map((item) => item.capability_id)).size, capabilities.length)
  for (const item of capabilities) {
    assert.ok(item.title?.trim())
    assert.ok(item.description?.trim())
    assert.ok(item.starter_prompt?.trim())
    assert.ok(workspaces.has(item.workspace), `${item.capability_id} has an unknown workspace`)
    assert.ok((item.intent_ids || []).every((intentId) => intents.has(intentId)))
    assert.ok((item.topic_ids || []).every((topicId) => topics.has(topicId)))
    assert.ok(Array.isArray(item.examples))
  }
})

test('Agent request/response contract versions are registered with strict schemas', () => {
  const policy = CONTRACT_REGISTRY.application_policy
  assert.equal(policy.agent_turn_contract_version, '1.1.0')
  assert.equal(CONTRACT_REGISTRY.agent_turn_context_schema.properties.schema_version.const, '1.1.0')
  assert.equal(CONTRACT_REGISTRY.agent_turn_response_schema.properties.schema_version.const, '1.1.0')
  assert.equal(CONTRACT_REGISTRY.agent_turn_context_schema.additionalProperties, false)
  assert.equal(CONTRACT_REGISTRY.agent_turn_response_schema.additionalProperties, false)
})
