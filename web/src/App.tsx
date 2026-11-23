import { ActionIcon, AppShell, Burger, Center, Group, Loader, Menu, NavLink, Text, Tooltip, useMantineColorScheme } from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import {
  IconActivity,
  IconBook2,
  IconChartBar,
  IconCpu,
  IconFolders,
  IconLayoutDashboard,
  IconListCheck,
  IconLogout,
  IconMessages,
  IconMoon,
  IconPlugConnected,
  IconSettings,
  IconSun,
  IconUser,
} from '@tabler/icons-react'
import { useEffect, type ComponentType } from 'react'
import { post, setUnauthorizedHandler } from './api'
import { Dot } from './components/common'
import Bench from './pages/Bench'
import Chat from './pages/Chat'
import Journal from './pages/Journal'
import Login from './pages/Login'
import Overview from './pages/Overview'
import Providers from './pages/Providers'
import Repos from './pages/Repos'
import Settings from './pages/Settings'
import Tasks from './pages/Tasks'
import Workers from './pages/Workers'
import { segments, useRoute } from './router'
import { loadMe, startLive, stopLive, useKite } from './store'

type Tab = { path: string; label: string; icon: ComponentType<{ size?: number; stroke?: number }>; page: ComponentType<{ parts: string[] }> }

const TABS: Tab[] = [
  { path: '', label: 'Tổng quan', icon: IconLayoutDashboard, page: Overview },
  { path: 'chat', label: 'Chat', icon: IconMessages, page: Chat },
  { path: 'tasks', label: 'Task', icon: IconListCheck, page: Tasks },
  { path: 'repos', label: 'Repo', icon: IconFolders, page: Repos },
  { path: 'providers', label: 'Provider', icon: IconPlugConnected, page: Providers },
  { path: 'workers', label: 'Worker', icon: IconCpu, page: Workers },
  { path: 'journal', label: 'Nhật ký', icon: IconBook2, page: Journal },
  { path: 'bench', label: 'Bench', icon: IconChartBar, page: Bench },
  { path: 'settings', label: 'Cài đặt', icon: IconSettings, page: Settings },
]

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
  const [opened, { toggle, close }] = useDisclosure()
  const path = useRoute((s) => s.path)
  const go = useRoute((s) => s.go)
  const me = useKite((s) => s.me)
  const connected = useKite((s) => s.connected)
  const gatewayUp = useKite((s) => s.gatewayUp)
  const { colorScheme, toggleColorScheme } = useMantineColorScheme()
  const parts = segments(path)
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
                  ? 'Mất kết nối trực tiếp với Kite (đang thử lại)'
                  : gatewayUp
                    ? 'Đang nối với Kite; gateway chạy'
                    : 'Đang nối với Kite; gateway không trả lời'
              }
            >
              <Group gap={6}>
                <Dot color={!connected ? 'gray' : gatewayUp ? 'teal' : 'red'} />
                <Text size="xs" c="dimmed" visibleFrom="xs">
                  {!connected ? 'mất kết nối' : gatewayUp ? 'gateway chạy' : 'gateway tắt'}
                </Text>
              </Group>
            </Tooltip>
          </Group>
          <Group gap="xs">
            <ActionIcon variant="subtle" onClick={toggleColorScheme} title="Đổi sáng/tối">
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
                  Tài khoản và cài đặt
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
                  Đăng xuất
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        {TABS.map((t) => (
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
            Kite không bao giờ push
          </Text>
        </Group>
      </AppShell.Navbar>
      <AppShell.Main>
        <PageComponent parts={parts.slice(1)} />
      </AppShell.Main>
    </AppShell>
  )
}
