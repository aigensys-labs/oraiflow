const IDENTIFIER_KEY = /(?:^|_)(?:id|digest|hash|reference)$/i

function stringifyValue(value) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try { return JSON.stringify(value) } catch { return '[unavailable]' }
}

function flattenEntries(value, prefix = '', depth = 0, output = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return output
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (child && typeof child === 'object' && !Array.isArray(child) && depth < 1) {
      flattenEntries(child, path, depth + 1, output)
    } else {
      const displayValue = stringifyValue(child)
      const isIdentifier = IDENTIFIER_KEY.test(key) || /(?:workflow|transaction|preview|approval)_id$/i.test(key)
      output.push({
        key: path,
        label: path.replace(/_/g, ' '),
        value: displayValue,
        wrap: isIdentifier || displayValue.length > 42,
      })
    }
  }
  return output
}

/** Convert audit JSON into stable key/value entries for readable rendering. */
export function auditDetailEntries(detail) {
  let value = detail
  if (typeof value === 'string') {
    try { value = JSON.parse(value) } catch {
      return value.trim() ? [{ key: 'details', label: 'details', value: value.trim(), wrap: value.length > 42 }] : []
    }
  }
  return flattenEntries(value && typeof value === 'object' ? value : {})
}
