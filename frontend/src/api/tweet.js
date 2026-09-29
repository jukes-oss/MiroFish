import axios from 'axios'

const client = axios.create({
  baseURL: '',
  timeout: 300000,
  headers: { 'Content-Type': 'application/json' },
})

function unwrap(error) {
  const message = error?.response?.data?.error || error?.response?.data?.message
  if (typeof message === 'string' && message) {
    error.message = message
  }
  return Promise.reject(error)
}

client.interceptors.response.use((response) => {
  const body = response.data
  if (body && body.success === false) {
    throw new Error(body.error || '请求没有成功')
  }
  return body
}, unwrap)

export function fetchHealth() {
  return client.get('/health')
}

export function listRuns() {
  return client.get('/api/tweet/runs')
}

export function createRun(payload) {
  return client.post('/api/tweet/runs', payload)
}

export function fetchRun(runId) {
  return client.get(`/api/tweet/runs/${encodeURIComponent(runId)}`)
}

export function cancelRun(runId) {
  return client.post(`/api/tweet/runs/${encodeURIComponent(runId)}/cancel`)
}

export function fetchReport(runId) {
  return client.get(`/api/tweet/runs/${encodeURIComponent(runId)}/report`)
}

export function deleteRun(runId) {
  return client.delete(`/api/tweet/runs/${encodeURIComponent(runId)}`)
}
