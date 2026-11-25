// The Repo tab was replaced by Environments (Gói 2). This stub only keeps
// the old route working: /repos redirects to /environments (old numeric repo
// ids do not map to environment ids, so detail links land on the list).
import { useEffect } from 'react'
import { go } from '../router'

export default function Repos() {
  useEffect(() => {
    go('/environments')
  }, [])
  return null
}
