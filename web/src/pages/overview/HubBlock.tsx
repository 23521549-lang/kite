import type { ReactNode } from 'react'
import { Card, Text } from '@mantine/core'

// Vỏ Card dùng chung cho các khối hub trên trang Tổng quan.
// Khối có `onClick` thì bấm được (class `clickable` + con trỏ tay);
// khối không có đích thì render thường, không giả vờ bấm được.
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
