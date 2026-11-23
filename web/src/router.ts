// A small history router: the back end serves index.html for every page path.
import { create } from 'zustand'

type Route = { path: string; go: (to: string, replace?: boolean) => void }

const current = () => location.pathname.replace(/\/+$/, '') || '/'

export const useRoute = create<Route>((set) => ({
  path: current(),
  go: (to, replace = false) => {
    if (to === current()) return
    if (replace) history.replaceState(null, '', to)
    else history.pushState(null, '', to)
    set({ path: current() })
    window.scrollTo(0, 0)
  },
}))

window.addEventListener('popstate', () => useRoute.setState({ path: current() }))

export function segments(path: string): string[] {
  return path.split('/').filter(Boolean).map(decodeURIComponent)
}

export function go(to: string) {
  useRoute.getState().go(to)
}
