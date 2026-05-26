// OWNS: applying a per-route title + meta description + OG tags to
//       <head> when a page mounts. Restores the previous values on
//       unmount so back-navigation doesn't strand a stale title.
// NOT OWNS: the actual copy strings (live in ./copy.js) or any
//       structured-data injection beyond og:title / og:description /
//       og:image / twitter:* mirrors.
// DECISIONS:
//   * No react-helmet-async dependency — manual document mutation
//     keeps the dependency graph small. Tradeoff: search-engine
//     crawlers see whatever the prerender pipeline emits; this hook
//     is for the hydrated SPA experience and for share-card previews
//     scraped by services that execute JS (Twitter scraper, etc.).
//   * Restore-on-unmount uses the captured pre-mount snapshot, NOT
//     a fixed default, so transitions A→B→A end up back at A's title
//     rather than the global default.

import { useEffect } from 'react'

function setOrCreateMeta(selector, attr, value) {
  if (value == null) return null
  let el = document.head.querySelector(selector)
  let createdHere = false
  if (!el) {
    el = document.createElement('meta')
    // The selector form is `meta[name="x"]` or `meta[property="og:x"]`.
    // Re-derive the attribute name + value from the selector itself.
    const match = selector.match(/^meta\[(name|property)="([^"]+)"\]$/)
    if (!match) return null
    el.setAttribute(match[1], match[2])
    document.head.appendChild(el)
    createdHere = true
  }
  const previous = el.getAttribute(attr)
  el.setAttribute(attr, value)
  return { el, attr, previous, createdHere }
}

export function usePageMeta({ title, description, ogImage }) {
  useEffect(() => {
    const previousTitle = document.title
    if (title) document.title = title

    const restorers = []
    const desc = description || null
    const og = ogImage || null
    if (desc) {
      restorers.push(setOrCreateMeta('meta[name="description"]', 'content', desc))
      restorers.push(setOrCreateMeta('meta[property="og:description"]', 'content', desc))
      restorers.push(setOrCreateMeta('meta[name="twitter:description"]', 'content', desc))
    }
    if (title) {
      restorers.push(setOrCreateMeta('meta[property="og:title"]', 'content', title))
      restorers.push(setOrCreateMeta('meta[name="twitter:title"]', 'content', title))
    }
    if (og) {
      restorers.push(setOrCreateMeta('meta[property="og:image"]', 'content', og))
      restorers.push(setOrCreateMeta('meta[name="twitter:image"]', 'content', og))
    }

    return () => {
      document.title = previousTitle
      for (const r of restorers) {
        if (!r) continue
        if (r.createdHere) {
          // We added this tag for this page; remove it cleanly on unmount.
          r.el.remove()
        } else if (r.previous != null) {
          r.el.setAttribute(r.attr, r.previous)
        }
      }
    }
  }, [title, description, ogImage])
}
