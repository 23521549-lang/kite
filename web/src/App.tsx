import { ActionIcon, AppShell, Burger, Center, Group, Loader, Menu, NavLink, SegmentedControl, Text, Tooltip, useMantineColorScheme } from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import {
  IconActivity,
  IconBriefcase,
  IconChartBar,
  IconCpu,
  IconFolders,
  IconLayoutDashboard,
  IconListCheck,
  IconLogout,
  IconMessages,
  IconMoon,
  IconSettings,
  IconSun,
  IconUser,
} from '@tabler/icons-react'
import { useEffect, type ComponentType } from 'react'
import { post, setUnauthorizedHandler } from './api'
import { Dot } from './components/common'
import { useLanguage, useT } from './i18n'
import Bench from './pages/Bench'
import Chat from './pages/Chat'
import Environments from './pages/Environments'
import Login from './pages/Login'
import Monitor from './pages/Monitor'
import Overview from './pages/Overview'
import Repos from './pages/Repos'
import Settings from './pages/Settings'
import Tasks from './pages/Tasks'
import Workers from './pages/Workers'
import Workspace from './pages/Workspace'
import { segments, useRoute } from './router'
import { loadMe, startLive, stopLive, useKite } from './store'

type Tab = { path: string; label: string; icon: ComponentType<{ size?: number; stroke?: number }>; page: ComponentType<{ parts: string[] }>; hidden?: boolean }

export default function App() {
  const me = useKite((s) => s.me)
  useEffect(() => {
    setUnauthorizedHandler(() => useKite.getState().setMe(null))
    loadMe()
  }, [])
  useEffect(() => {
    if (me) startLive()
    else stopLive()
  }, [me])
  if (me === undefined)
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    )
  if (me === null) return <Login />
  return <Shell />
}

function Shell() {
  const t = useT()
  const { language, setLanguage } = useLanguage()
  const TABS: Tab[] = [
    { path: '', label: t('nav.overview'), icon: IconLayoutDashboard, page: Overview },
    { path: 'workspace', label: t('nav.workspace'), icon: IconBriefcase, page: Workspace },
    { path: 'chat', label: t('nav.chat'), icon: IconMessages, page: Chat },
    { path: 'tasks', label: t('nav.tasks'), icon: IconListCheck, page: Tasks },
    { path: 'environments', label: t('nav.environments'), icon: IconFolders, page: Environments },
    { path: 'repos', label: t('nav.repos'), icon: IconFolders, page: Repos, hidden: true },
    { path: 'workers', label: t('nav.workers'), icon: IconCpu, page: Workers },
    { path: 'bench', label: t('nav.bench'), icon: IconChartBar, page: Bench },
    { path: 'giam-sat', label: t('nav.monitor'), icon: IconActivity, page: Monitor },
    { path: 'settings', label: t('nav.settings'), icon: IconSettings, page: Settings },
  ]
  const [opened, { toggle, close }] = useDisclosure()
  const path = useRoute((s) => s.path)
  const go = useRoute((s) => s.go)
  const me = useKite((s) => s.me)
  const connected = useKite((s) => s.connected)
  const gatewayUp = useKite((s) => s.gatewayUp)
  const { colorScheme, toggleColorScheme } = useMantineColorScheme()
  const parts = segments(path)
  const isProvidersRedirect = parts[0] === 'providers'
  useEffect(() => {
    if (isProvidersRedirect) go('/workers', true)
  }, [isProvidersRedirect, go])
  const tab = TABS.find((t) => t.path === (parts[0] ?? '')) ?? TABS[0]
  const PageComponent = tab.page
  const isChat = tab.path === 'chat'

  return (
    <AppShell
      header={{ height: 52 }}
      navbar={{ width: 210, breakpoint: 'sm', collapsed: { mobile: !opened } }}
      padding={isChat ? 0 : 'md'}
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="sm">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" />
            <Text fw={700} size="lg">
              Kite
            </Text>
            <Tooltip
              label={
                !connected
                  ? t('header.connection-lost')
                  : gatewayUp
                    ? t('header.gateway-running')
                    : t('header.gateway-down')
              }
            >
              <Group gap={6}>
                <Dot color={!connected ? 'gray' : gatewayUp ? 'teal' : 'red'} />
                <Text size="xs" c="dimmed" visibleFrom="xs">
                  {!connected ? t('header.status-offline') : gatewayUp ? t('header.status-gateway-running') : t('header.status-gateway-down')}
                </Text>
              </Group>
            </Tooltip>
          </Group>
          <Group gap="xs">
            <SegmentedControl
              size="xs"
              data={[
                { value: 'vi', label: 'VI' },
                { value: 'en', label: 'EN' },
              ]}
              value={language}
              onChange={(v) => setLanguage(v === 'en' ? 'en' : 'vi')}
              aria-label={t('header.language')}
            />
            <ActionIcon variant="subtle" onClick={toggleColorScheme} title={t('header.toggle-theme')}>
              {colorScheme === 'dark' ? <IconSun size={18} /> : <IconMoon size={18} />}
            </ActionIcon>
            <Menu position="bottom-end">
              <Menu.Target>
                <ActionIcon variant="subtle" title={me?.username}>
                  <IconUser size={18} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label>{me?.username}</Menu.Label>
                <Menu.Item leftSection={<IconSettings size={14} />} onClick={() => go('/settings')}>
                  {t('header.account-settings')}
                </Menu.Item>
                <Menu.Item
                  color="red"
                  leftSection={<IconLogout size={14} />}
                  onClick={async () => {
                    try {
                      await post('/api/auth/logout')
                    } finally {
                      useKite.getState().setMe(null)
                    }
                  }}
                >
                  {t('header.logout')}
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        {TABS.filter((t) => !t.hidden).map((t) => (
          <NavLink
            key={t.path}
            href={`/${t.path}`}
            label={t.label}
            leftSection={<t.icon size={18} stroke={1.6} />}
            active={t === tab}
            onClick={(e) => {
              e.preventDefault()
              go(`/${t.path}`)
              close()
            }}
          />
        ))}
        <Group mt="auto" gap={6} px="xs" pb={4}>
          <IconActivity size={14} color="var(--mantine-color-dimmed)" />
          <Text size="xs" c="dimmed">
            {t('header.never-push')}
          </Text>
        </Group>
      </AppShell.Navbar>
      <AppShell.Main>
        <PageComponent parts={parts.slice(1)} />
      </AppShell.Main>
    </AppShell>
  )
}
