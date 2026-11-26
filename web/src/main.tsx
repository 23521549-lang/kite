import '@mantine/core/styles.css'
import '@mantine/notifications/styles.css'
import './app.css'
import { MantineProvider, createTheme } from '@mantine/core'
import { Notifications } from '@mantine/notifications'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { LanguageProvider } from './i18n'

const theme = createTheme({
  primaryColor: 'indigo',
  defaultRadius: 'md',
  fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  fontFamilyMonospace: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <Notifications position="top-right" limit={4} />
      <LanguageProvider>
        <App />
      </LanguageProvider>
    </MantineProvider>
  </StrictMode>,
)
