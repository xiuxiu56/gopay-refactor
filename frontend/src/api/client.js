export class ApiError extends Error {
  constructor(message, status = 0, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((part) => part.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : ''
}

function isMutation(method) {
  return ['POST', 'PUT', 'PATCH', 'DELETE'].includes(String(method || 'GET').toUpperCase())
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {})
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (isMutation(options.method)) {
    const csrfToken = cookieValue('gopay_v2_csrf')
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
  }

  let response
  try {
    response = await fetch(path, {
      credentials: 'same-origin',
      cache: path.startsWith('/api/') ? 'no-store' : 'default',
      ...options,
      headers,
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw new ApiError('请求已取消')
    throw new ApiError('连接本地服务失败，请检查服务是否已经启动')
  }

  let payload
  try {
    payload = await response.json()
  } catch {
    throw new ApiError(`服务返回了无效响应（HTTP ${response.status}）`, response.status)
  }

  if (!response.ok || payload?.success === false) {
    const authPath = path.startsWith('/api/v1/auth/')
    if (response.status === 401 && !authPath && window.location.pathname !== '/login') {
      const redirect = `${window.location.pathname}${window.location.search}`
      window.location.replace(`/login?redirect=${encodeURIComponent(redirect)}`)
    }
    throw new ApiError(
      payload?.message || `请求失败（HTTP ${response.status}）`,
      response.status,
      payload?.code || '',
    )
  }
  return payload?.data ?? payload
}

export function queryString(values) {
  const params = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) params.set(key, String(value))
  })
  const text = params.toString()
  return text ? `?${text}` : ''
}
