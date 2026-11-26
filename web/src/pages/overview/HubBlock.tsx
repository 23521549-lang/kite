import type { ReactNode } from 'react'
import { Card, Text } from '@mantine/core'

// Shared Card shell for the hub blocks on the Overview page.
// A block with `onClick` is clickable (`clickable` class + pointer cursor);
// a block without a target renders plain, never pretending to be clickable.
export default function HubBlock({ title, onClick, children }: { title: string; onClick?: () => void; children: ReactNode }) {
  return (
    <Card withBorder className={onClick ? 'clickable' : undefined} onClick={onClick}>
      <Text fw={600} mb={6}>
        {title}
      </Text>
      {children}
    </Card>
  )
}
