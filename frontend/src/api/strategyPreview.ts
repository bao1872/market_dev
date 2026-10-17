/** Selection is untrusted URL input; the server authorizes and verifies every preview. */
export function strategyPreviewKey(): string {
  return typeof window === 'undefined' ? '' : new URLSearchParams(window.location.search).get('preview_batch') ?? ''
}
export function strategyPreviewParams(): {preview_batch?: string} {
  const id = strategyPreviewKey()
  return id ? {preview_batch:id} : {}
}
