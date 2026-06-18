// ---------------------------------------------------------------------------
// Transfera v2 — Theme Hook
// Reads/writes "light" | "dark" to localStorage, syncs .dark class on <html>.
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useState } from 'react'

type Theme = 'light' | 'dark'

const STORAGE_KEY = 'mv-theme'

function getStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'dark' || stored === 'light') return stored
  } catch { /* SSR / private browsing */ }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function applyTheme(theme: Theme) {
  const root = document.documentElement
  if (theme === 'dark') {
    root.classList.add('dark')
  } else {
    root.classList.remove('dark')
  }
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(getStoredTheme)

  // Apply on mount
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next)
    try { localStorage.setItem(STORAGE_KEY, next) } catch { /* ignore */ }
    applyTheme(next)
  }, [])

  const toggleTheme = useCallback(() => {
    const next: Theme = getStoredTheme() === 'dark' ? 'light' : 'dark'
    setTheme(next)
  }, [setTheme])

  return { theme, setTheme, toggleTheme }
}
