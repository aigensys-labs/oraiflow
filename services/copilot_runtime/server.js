import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { createRequire } from 'node:module'
import { CopilotRuntime, InMemoryAgentRunner } from '@copilotkit/runtime/v2'
import { createCopilotNodeListener } from '@copilotkit/runtime/v2/node'
import { HttpAgent } from '@ag-ui/client'

const require = createRequire(import.meta.url)
const { version } = require('./package.json')

const port = Number(process.env.PORT || 3001)
const agentUrl = process.env.AGENT_AGUI_URL || 'http://agent-runtime:8101/agui'
const agentToken = process.env.AGENT_RUNTIME_INTERNAL_TOKEN || ''

if (!agentToken) throw new Error('AGENT_RUNTIME_INTERNAL_TOKEN is required')

const runtime = new CopilotRuntime({
  agents: {
    oraiflow_agent: new HttpAgent({
      url: agentUrl,
      headers: { Authorization: `Bearer ${agentToken}` },
    }),
  },
  runner: new InMemoryAgentRunner(),
  // This 0.7.x reference framework is a single-user local sandbox. Scope runtime thread endpoints to
  // the configured demo identity; replace this with a verified app session
  // before any shared or production deployment. Never use threads: { access: "open" }.
  identifyUser: async () => ({
    id: process.env.DEMO_USER_ID || 'user-001',
    name: process.env.DEMO_DISPLAY_NAME || 'Demo Customer',
  }),
})

const listener = createCopilotNodeListener({ runtime, basePath: '/api/copilotkit' })
const server = createServer((req, res) => {
  if (req.url === '/healthz') { res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"status":"ok"}'); return }
  const isAgentRun = req.method === 'POST' && (req.url || '').includes('/agent/oraiflow_agent/run')
  const startedAt = performance.now()
  const requestId = isAgentRun ? randomUUID().slice(0, 12) : null
  if (isAgentRun) {
    console.info(`[copilot-runtime] agent_run_started request_id=${requestId}`)
    res.once('finish', () => {
      console.info(`[copilot-runtime] agent_run_response request_id=${requestId} status=${res.statusCode} duration_ms=${Math.round(performance.now() - startedAt)}`)
    })
    res.once('close', () => {
      if (!res.writableFinished) console.warn(`[copilot-runtime] agent_run_client_closed request_id=${requestId} duration_ms=${Math.round(performance.now() - startedAt)}`)
    })
  }
  listener(req, res)
})

server.listen(port, '0.0.0.0', () => {
  console.log(`[copilot-runtime] v${version} listening on ${port}; AG-UI agent=${agentUrl}`)
})
