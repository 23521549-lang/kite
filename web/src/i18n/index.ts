import { createContext, createElement, useContext, useMemo, useState, type ReactNode } from 'react'
import { en } from './en'
import { vi, type DictKey } from './vi'

export type { DictKey }

export type Language = 'vi' | 'en'

/** The t() function: translate a static key, with optional {var} interpolation. */
export type TFunction = (key: DictKey, vars?: Record<string, string | number>) => string

export const LANGUAGE_STORAGE_KEY = 'kite-language'

const dicts: Record<Language, Record<DictKey, string>> = { vi, en }

function readStoredLanguage(): Language {
  try {
    const value = localStorage.getItem(LANGUAGE_STORAGE_KEY)
    if (value === 'vi' || value === 'en') return value
  } catch {
    /* no storage: the default */
  }
  return 'vi'
}

const LanguageContext = createContext<{ language: Language; setLanguage: (next: Language) => void } | null>(null)

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(readStoredLanguage)
  const value = useMemo(
    () => ({
      language,
      setLanguage(next: Language) {
        setLanguageState(next)
        try {
          localStorage.setItem(LANGUAGE_STORAGE_KEY, next)
        } catch {
          /* the choice lasts for this visit only */
        }
      },
    }),
    [language],
  )
  // No JSX here: this file stays index.ts (per the Gói 0(b) contract).
  return createElement(LanguageContext.Provider, { value }, children)
}

export function useLanguage() {
  const ctx = useContext(LanguageContext)
  if (!ctx) throw new Error('useLanguage must be used inside LanguageProvider')
  return ctx
}

/** Read the current language without a hook — for code outside React
 *  (e.g. util.ts error helpers). Reads the same 'kite-language' key the
 *  provider persists; defaults to 'vi' when nothing is stored. */
export function getLanguage(): Language {
  return readStoredLanguage()
}

/** Translate a key without a hook — for code outside React (e.g. the 3D
 *  office scene). Same lookup + fallback as useT(); callers that cache the
 *  result must re-run it when the language changes. */
export function translate(key: DictKey, vars?: Record<string, string | number>): string {
  const out = dicts[getLanguage()][key] ?? vi[key] ?? key
  if (!vars) return out
  return Object.entries(vars).reduce((s, [k, v]) => s.split(`{${k}}`).join(String(v)), out)
}

/** Translate a key. Falls back to the Vietnamese string when the English one is
 *  missing — the UI never renders an empty string. A missing key is still a bug:
 *  the dictionaries share one key set, enforced by TypeScript. */
export function useT(): TFunction {
  const { language } = useLanguage()
  return (key, vars) => {
    const out = dicts[language][key] ?? vi[key] ?? key
    if (!vars) return out
    return Object.entries(vars).reduce((s, [k, v]) => s.split(`{${k}}`).join(String(v)), out)
  }
}
