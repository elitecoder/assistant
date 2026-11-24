// Resolve @slack/* to local stubs so index.js can boot with no node_modules.
const STUBS = {
  '@slack/bolt': new URL('./stub-bolt.mjs', import.meta.url).href,
  '@slack/web-api': new URL('./stub-web-api.mjs', import.meta.url).href,
}

export async function resolve(specifier, context, next) {
  if (STUBS[specifier]) return { url: STUBS[specifier], shortCircuit: true }
  return next(specifier, context)
}
