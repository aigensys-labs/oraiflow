import { z } from 'zod'
import { CONTRACT_REGISTRY } from './generated/contracts.js'

// Shared product metadata: the browser and Agent Runtime consume the same
// versioned workspace and Academy catalogues from the OCCF registry.
export const WORKSPACE_CATALOGUE = Object.freeze(CONTRACT_REGISTRY.application_policy?.workspaces || {})
export const ACADEMY_TOPICS = Object.freeze(CONTRACT_REGISTRY.application_policy?.academy_topics || [])
export const WORKSPACE_CATALOGUE_VERSION = CONTRACT_REGISTRY.application_policy?.workspace_catalogue_version || '1.0.0'
export const ACADEMY_CATALOGUE_VERSION = CONTRACT_REGISTRY.application_policy?.academy_catalogue_version || '1.0.0'
export const ASSISTANT_CAPABILITIES = Object.freeze(CONTRACT_REGISTRY.application_policy?.assistant_capabilities || [])
export const ASSISTANT_CAPABILITY_CATALOGUE_VERSION = CONTRACT_REGISTRY.application_policy?.assistant_capability_catalogue_version || '1.0.0'
export const AGENT_TURN_CONTRACT_VERSION = CONTRACT_REGISTRY.application_policy?.agent_turn_contract_version || '1.1.0'

function resolveLocalSchema(schema, stack = []) {
  if (Array.isArray(schema)) return schema.map((item) => resolveLocalSchema(item, stack))
  if (!schema || typeof schema !== 'object') return schema
  if (typeof schema.$ref === 'string') {
    const reference = schema.$ref
    if (!reference.startsWith('#/')) throw new Error(`Unsupported external contract reference: ${reference}`)
    if (stack.includes(reference)) throw new Error(`Recursive contract reference: ${reference}`)
    const target = reference.slice(2).split('/').reduce((value, part) => {
      const key = part.replace(/~1/g, '/').replace(/~0/g, '~')
      return value?.[key]
    }, CONTRACT_REGISTRY)
    if (!target) throw new Error(`Unresolved contract reference: ${reference}`)
    const resolved = resolveLocalSchema(target, [...stack, reference])
    const siblings = Object.fromEntries(Object.entries(schema).filter(([key]) => key !== '$ref'))
    return Object.keys(siblings).length
      ? { allOf: [resolved, resolveLocalSchema(siblings, stack)] }
      : resolved
  }
  return Object.fromEntries(Object.entries(schema).map(([key, value]) => [key, resolveLocalSchema(value, stack)]))
}

function jsonSchemaToZod(inputSchema) {
  const schema = resolveLocalSchema(inputSchema)
  if (!schema || typeof schema !== 'object') return z.unknown()
  if (schema.const !== undefined) return z.literal(schema.const)
  if (Array.isArray(schema.enum)) return z.enum(schema.enum)

  if (Array.isArray(schema.oneOf) || Array.isArray(schema.anyOf)) {
    const variants = schema.oneOf || schema.anyOf
    return z.union(variants.map(jsonSchemaToZod))
  }
  if (Array.isArray(schema.allOf)) {
    return schema.allOf.map(jsonSchemaToZod).reduce((left, right) => z.intersection(left, right))
  }
  if (Array.isArray(schema.type)) {
    const variants = schema.type.map((type) => jsonSchemaToZod({ ...schema, type, enum: undefined }))
    return z.union(variants)
  }

  let result
  switch (schema.type) {
    case 'object': {
      const properties = schema.properties || {}
      const required = new Set(schema.required || [])
      const shape = Object.fromEntries(Object.entries(properties).map(([key, value]) => {
        const child = jsonSchemaToZod(value)
        return [key, required.has(key) ? child : child.optional()]
      }))
      result = z.object(shape)
      if (schema.additionalProperties === false) result = result.strict()
      else if (schema.additionalProperties === true) result = result.passthrough()
      break
    }
    case 'array':
      result = z.array(jsonSchemaToZod(schema.items || {}))
      if (Number.isInteger(schema.minItems)) result = result.min(schema.minItems)
      if (Number.isInteger(schema.maxItems)) result = result.max(schema.maxItems)
      break
    case 'string':
      result = z.string()
      if (Number.isInteger(schema.minLength)) result = result.min(schema.minLength)
      if (Number.isInteger(schema.maxLength)) result = result.max(schema.maxLength)
      if (typeof schema.pattern === 'string') result = result.regex(new RegExp(schema.pattern))
      if (schema.format === 'date-time') result = result.datetime({ offset: true })
      break
    case 'integer': result = z.number().int(); break
    case 'number': result = z.number(); break
    case 'boolean': result = z.boolean(); break
    case 'null': result = z.null(); break
    default: result = z.unknown()
  }

  if ((schema.type === 'number' || schema.type === 'integer') && typeof schema.minimum === 'number') result = result.min(schema.minimum)
  if ((schema.type === 'number' || schema.type === 'integer') && typeof schema.maximum === 'number') result = result.max(schema.maximum)
  if (schema.default !== undefined) result = result.default(schema.default)
  return result
}

export function capabilitySchema(capabilityId, section = 'input') {
  const contract = CONTRACT_REGISTRY.capabilities?.[capabilityId]
  if (!contract) throw new Error(`Unknown capability contract: ${capabilityId}`)
  const schema = section === 'output' ? contract.output_schema : contract.input_schema
  return jsonSchemaToZod(schema)
}

export function presentationSchema(presentationId) {
  const contract = CONTRACT_REGISTRY.presentations?.[presentationId]
  if (!contract) throw new Error(`Unknown presentation contract: ${presentationId}`)
  return jsonSchemaToZod(contract.props_schema)
}

export function presentationContract(presentationId) {
  const contract = CONTRACT_REGISTRY.presentations?.[presentationId]
  if (!contract) throw new Error(`Unknown presentation contract: ${presentationId}`)
  return contract
}

export function resolveWorkspace(value) {
  if (typeof value !== 'string') return null
  const key = value.trim().toLowerCase().replace(/\s+/g, ' ')
  const page = CONTRACT_REGISTRY.application_policy?.navigation?.aliases?.[key]
  return CONTRACT_REGISTRY.application_policy?.navigation?.pages?.includes(page) ? page : null
}

class FrontendPreconditionError extends Error {
  constructor(conditionId) {
    super(`Frontend precondition failed: ${conditionId}`)
    this.name = 'FrontendPreconditionError'
    this.conditionId = conditionId
  }
}

function evaluateFrontendPreconditions(contract, input, context) {
  for (const condition of contract.preconditions || []) {
    if (condition.enforced_by !== 'frontend') continue
    let passed = false
    switch (condition.evaluator) {
      case 'workspace_allowlist':
        passed = resolveWorkspace(input[condition.argument]) !== null
        break
      case 'context_field_present':
        passed = context && context[condition.context_key] !== undefined && context[condition.context_key] !== null
        break
      case 'context_collection_contains_argument': {
        const collection = context?.[condition.context_key]
        passed = Array.isArray(collection) && collection.some((item) => item?.[condition.item_key] === input[condition.argument])
        break
      }
      default:
        throw new Error(`Unsupported frontend precondition evaluator: ${condition.evaluator}`)
    }
    if (!passed) throw new FrontendPreconditionError(condition.id)
  }
}

export function validateApplicationContext(value) {
  const schema = CONTRACT_REGISTRY.application_context?.schema
  if (!schema) throw new Error('Application context contract is missing')
  const parsed = jsonSchemaToZod(schema).parse(value)
  for (const rule of CONTRACT_REGISTRY.application_context.semantic_rules || []) {
    switch (rule.evaluator) {
      case 'unique_collection_property': {
        const values = parsed[rule.collection].map((item) => item[rule.property])
        if (new Set(values).size !== values.length) throw new Error(`Application context semantic rule failed: ${rule.id}`)
        break
      }
      case 'selected_record_matches': {
        const selectedValue = parsed[rule.selector]
        if (selectedValue === null) break
        const selected = parsed[rule.collection].find((item) => item[rule.record_key] === selectedValue)
        if (!selected || Object.entries(rule.field_mappings).some(([contextField, itemField]) => parsed[contextField] !== selected[itemField])) {
          throw new Error(`Application context semantic rule failed: ${rule.id}`)
        }
        break
      }
      default:
        throw new Error(`Unsupported application context semantic evaluator: ${rule.evaluator}`)
    }
  }
  return parsed
}

function resultEnvelopeSchema() {
  return jsonSchemaToZod(CONTRACT_REGISTRY.result_envelope_schema)
}

function newTraceId() {
  return globalThis.crypto?.randomUUID?.() || `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function validateResultEnvelope(capabilityId, envelope) {
  const parsed = resultEnvelopeSchema().parse(envelope)
  const contract = CONTRACT_REGISTRY.capabilities?.[capabilityId]
  if (!contract || parsed.capability !== capabilityId) throw new Error('Capability result identity mismatch')
  if (['SUCCESS', 'EMPTY', 'PARTIAL'].includes(parsed.outcome)) {
    capabilitySchema(capabilityId, 'output').parse(parsed.data)
    if (!parsed.evidence || parsed.error !== null || parsed.trust_class !== 'untrusted_banking_data') {
      throw new Error('Successful result has inconsistent evidence or trust metadata')
    }
    if (parsed.evidence.source !== contract.evidence?.source || parsed.evidence.scope !== contract.evidence?.scope) {
      throw new Error('Result provenance does not match the capability declaration')
    }
    if (parsed.evidence.trace_id !== parsed.trace_id) throw new Error('Result trace identifier mismatch')
    const empty = Array.isArray(parsed.data) ? parsed.data.length === 0 : parsed.data && typeof parsed.data === 'object' && Object.keys(parsed.data).length === 0
    if (parsed.outcome === 'EMPTY' && (!empty || parsed.evidence.completeness !== 'EMPTY')) throw new Error('EMPTY result invariants failed')
    if (parsed.outcome === 'PARTIAL' && (empty || parsed.evidence.completeness !== 'PARTIAL')) throw new Error('PARTIAL result invariants failed')
    if (parsed.outcome === 'SUCCESS' && (empty || ['EMPTY', 'PARTIAL'].includes(parsed.evidence.completeness))) throw new Error('SUCCESS result invariants failed')
  } else {
    if (parsed.data !== null || parsed.evidence !== null || parsed.trust_class !== 'connector_error' || !parsed.error?.code) {
      throw new Error('Failed result invariants failed')
    }
  }
  return parsed
}

export async function executeFrontendCapability(capabilityId, args, handler, options = {}) {
  const contract = CONTRACT_REGISTRY.capabilities?.[capabilityId]
  if (!contract || contract.kind !== 'frontend' || contract.authorization?.agent_invocable !== true) {
    throw new Error(`Not an agent-invocable frontend capability: ${capabilityId}`)
  }
  const traceId = newTraceId()
  const inputSchema = capabilitySchema(capabilityId, 'input')
  const outputSchema = capabilitySchema(capabilityId, 'output')

  let input
  try {
    input = inputSchema.parse(args ?? {})
  } catch {
    return validateResultEnvelope(capabilityId, {
      schema_version: '1.0.0', capability: capabilityId, outcome: 'INVALID_INPUT', data: null,
      evidence: null, error: { code: 'INVALID_INPUT', retryable: false }, trace_id: traceId,
      trust_class: 'connector_error',
    })
  }

  try {
    const context = options.context === undefined ? null : validateApplicationContext(options.context)
    evaluateFrontendPreconditions(contract, input, context)
  } catch (error) {
    const preconditionId = error instanceof FrontendPreconditionError ? error.conditionId : 'APPLICATION_CONTEXT_INVALID'
    return validateResultEnvelope(capabilityId, {
      schema_version: '1.0.0', capability: capabilityId, outcome: 'REJECTED', data: null,
      evidence: null, error: { code: 'PRECONDITION_FAILED', retryable: false, message: `The application could not complete this action because a required condition was not met (${preconditionId}).` },
      trace_id: traceId, trust_class: 'connector_error',
    })
  }

  let rawResult
  try {
    rawResult = await handler(input)
  } catch {
    return validateResultEnvelope(capabilityId, {
      schema_version: '1.0.0', capability: capabilityId, outcome: 'UNAVAILABLE', data: null,
      evidence: null, error: { code: 'FRONTEND_ACTION_FAILED', retryable: false }, trace_id: traceId,
      trust_class: 'connector_error',
    })
  }

  let data
  try {
    data = outputSchema.parse(rawResult)
  } catch {
    return validateResultEnvelope(capabilityId, {
      schema_version: '1.0.0', capability: capabilityId, outcome: 'INVALID_RESPONSE', data: null,
      evidence: null, error: { code: 'INVALID_FRONTEND_ACTION_RESULT', retryable: false }, trace_id: traceId,
      trust_class: 'connector_error',
    })
  }
  const rejected = data.status === 'rejected'
  return validateResultEnvelope(capabilityId, {
    schema_version: '1.0.0', capability: capabilityId, outcome: rejected ? 'REJECTED' : 'SUCCESS',
    data: rejected ? null : data,
    evidence: rejected ? null : {
      source: contract.evidence?.source || 'frontend', scope: contract.evidence?.scope || 'current_application_session',
      retrieved_at: new Date().toISOString(), completeness: 'COMPLETE', trace_id: traceId,
    },
    error: rejected ? { code: 'ACTION_REJECTED', retryable: false, message: data.reason || 'The application rejected the action.' } : null,
    trace_id: traceId, trust_class: rejected ? 'connector_error' : 'untrusted_banking_data',
  })
}

export function unwrapCapabilityResult(value) {
  if (value && typeof value === 'object' && value.schema_version === '1.0.0' && value.data && typeof value.data === 'object') {
    return value.data
  }
  return value
}
