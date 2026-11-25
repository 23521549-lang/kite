// The 3D office of the Overview tab, drawn with three.js from simple shapes (no model files): one desk
// and robot per worker, the lead agent's desk in front, the gateway rack at the back, the queue of
// waiting tasks by the door. Tasks float above the desk of the worker that runs them, a line joins the
// lead desk to every worker with a task, and each model request flies from the gateway (or from the lead
// desk for the chat) to the worker that answered it.
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { disposeSprite, labelSize, makeLabel, sizeLabel, type Line } from './labels'

export type WorkerState = 'idle' | 'busy' | 'starting' | 'off' | 'error'
export type WorkerView = {
  id: string
  type: string
  state: WorkerState
  stateLabel: string
  model: string
  running: number
  slots: number
}
export type TaskView = { id: string; goal: string; worker: string | null; step: string | null }
export type OfficeState = { workers: WorkerView[]; tasks: TaskView[]; chatBusy: boolean; gatewayUp: boolean; dark: boolean }
export type Pick = { kind: 'worker' | 'task' | 'lead' | 'gateway' | 'queue'; id: string }
// Mọi chuỗi hiển thị trong scene 3D — do component React truyền vào (đã dịch
// qua `overview.*`); scene không tự hard-code chuỗi UI, không tự đọc ngôn ngữ.
export type OfficeLabels = {
  moreTasks: (n: number) => string
  openTask: string
  aria: string
  queueTip: string
  gatewayTip: string
  gatewayUp: string
  gatewayDown: string
  workerTip: string
  runningSuffix: (running: number, slots: number) => string
  leadTip: string
  leadName: string
  answering: string
  waiting: string
  queueName: string
  queueWaiting: (n: number) => string
  queueEmpty: string
}
type Handlers = { onPick: (pick: Pick) => void; onHover: (tip: string | null, x: number, y: number) => void; labels: OfficeLabels }

const STATE_COLOR: Record<WorkerState, number> = {
  idle: 0x20c997,
  busy: 0x5c7cfa,
  starting: 0xfab005,
  off: 0x868e96,
  error: 0xfa5252,
}
// Darker shades for text on the light theme's white labels.
const STATE_TEXT_LIGHT: Record<WorkerState, number> = {
  idle: 0x099268,
  busy: 0x3b5bdb,
  starting: 0xd9480f,
  off: 0x495057,
  error: 0xe03131,
}
const TYPE_COLOR: Record<string, number> = { kaggle: 0x22b8cf, openai: 0x51cf66, anthropic: 0xff922b, runpod: 0xcc5de8 }
const LEAD_COLOR = 0x9775fa
const LEAD_POS = new THREE.Vector3(0, 0, 4.6)
const LABEL_Y = 2.1 // labels sit just above a robot's antenna
const MAX_CARDS = 2 // task cards shown above one desk
const COMPACT_WIDTH = 640 // narrower than this (phones): smaller labels, no task cards

type Shared = {
  desk: THREE.Material
  metal: THREE.Material
  chair: THREE.Material
  dark: THREE.Material
  eye: THREE.Material
  boxGeo: THREE.BoxGeometry
}

function mesh(geo: THREE.BufferGeometry, mat: THREE.Material, shadows: boolean): THREE.Mesh {
  const m = new THREE.Mesh(geo, mat)
  m.castShadow = shadows
  m.receiveShadow = shadows
  return m
}

function box(shared: Shared, w: number, h: number, d: number, mat: THREE.Material, shadows: boolean): THREE.Mesh {
  const m = mesh(shared.boxGeo, mat, shadows)
  m.scale.set(w, h, d)
  return m
}

/** One desk with its robot: a worker, or the lead agent. */
class Station {
  group = new THREE.Group()
  robot = new THREE.Group()
  head: THREE.Mesh
  bulb: THREE.Mesh
  ring: THREE.Mesh
  stripe: THREE.Mesh
  hit: THREE.Mesh
  label: THREE.Sprite | null = null
  labelKey = ''
  cards: THREE.Sprite[] = []
  cardsKey = ''
  zzz: THREE.Sprite | null = null
  state: WorkerState = 'idle'
  phase = Math.random() * 10
  private bulbMat: THREE.MeshStandardMaterial
  private ringMat: THREE.MeshBasicMaterial
  private stripeMat: THREE.MeshStandardMaterial
  private bodyMat: THREE.MeshStandardMaterial

  constructor(
    readonly pick: Pick,
    color: number,
    shared: Shared,
    shadows: boolean,
    wide = false,
  ) {
    const g = this.group
    const deskW = wide ? 2.8 : 2.2
    const top = box(shared, deskW, 0.08, 1.1, shared.desk, shadows)
    top.position.set(0, 0.78, 0)
    g.add(top)
    for (const [x, z] of [
      [-deskW / 2 + 0.1, -0.45],
      [deskW / 2 - 0.1, -0.45],
      [-deskW / 2 + 0.1, 0.45],
      [deskW / 2 - 0.1, 0.45],
    ]) {
      const leg = box(shared, 0.07, 0.76, 0.07, shared.metal, shadows)
      leg.position.set(x, 0.38, z)
      g.add(leg)
    }
    // Monitor: its screen faces the robot; its back, which the camera sees, carries a strip in the state colour.
    const monitor = box(shared, 1.0, 0.6, 0.06, shared.dark, shadows)
    monitor.position.set(0, 1.18, -0.05)
    g.add(monitor)
    const stand = box(shared, 0.08, 0.3, 0.08, shared.metal, shadows)
    stand.position.set(0, 0.95, -0.05)
    g.add(stand)
    this.stripeMat = new THREE.MeshStandardMaterial({ color: 0x000000, emissive: STATE_COLOR.idle, emissiveIntensity: 0.9 })
    this.stripe = box(shared, 0.86, 0.06, 0.02, this.stripeMat, false)
    this.stripe.position.set(0, 1.0, -0.01)
    g.add(this.stripe)
    const keyboard = box(shared, 0.7, 0.03, 0.22, shared.dark, shadows)
    keyboard.position.set(0, 0.835, -0.36)
    g.add(keyboard)
    // Chair.
    const seat = box(shared, 0.7, 0.08, 0.62, shared.chair, shadows)
    seat.position.set(0, 0.46, -0.95)
    g.add(seat)
    const back = box(shared, 0.7, 0.7, 0.08, shared.chair, shadows)
    back.position.set(0, 0.86, -1.27)
    g.add(back)
    const post = box(shared, 0.07, 0.42, 0.07, shared.metal, shadows)
    post.position.set(0, 0.21, -0.95)
    g.add(post)
    // Robot.
    this.bodyMat = new THREE.MeshStandardMaterial({ color, roughness: 0.45, metalness: 0.15 })
    const body = mesh(new THREE.CapsuleGeometry(0.27, 0.42, 6, 14), this.bodyMat, shadows)
    body.position.set(0, 0.92, 0)
    this.robot.add(body)
    this.head = mesh(new THREE.SphereGeometry(0.26, 20, 16), this.bodyMat, shadows)
    this.head.position.set(0, 1.52, 0)
    this.robot.add(this.head)
    for (const x of [-0.09, 0.09]) {
      const eye = mesh(new THREE.SphereGeometry(0.045, 10, 8), shared.eye, false)
      eye.position.set(x, 0.04, 0.23)
      this.head.add(eye)
    }
    const antenna = box(shared, 0.025, 0.2, 0.025, shared.metal, false)
    antenna.position.set(0, 0.33, 0)
    this.head.add(antenna)
    this.bulbMat = new THREE.MeshStandardMaterial({ color: 0x000000, emissive: STATE_COLOR.idle, emissiveIntensity: 1.4 })
    this.bulb = mesh(new THREE.SphereGeometry(0.07, 12, 10), this.bulbMat, false)
    this.bulb.position.set(0, 0.46, 0)
    this.head.add(this.bulb)
    this.robot.position.set(0, 0, -0.95)
    g.add(this.robot)
    // Ring on the floor, in the state colour.
    this.ringMat = new THREE.MeshBasicMaterial({ color: STATE_COLOR.idle, transparent: true, opacity: 0.75 })
    this.ring = new THREE.Mesh(new THREE.RingGeometry(wide ? 1.85 : 1.55, wide ? 2.0 : 1.7, 56), this.ringMat)
    this.ring.rotation.x = -Math.PI / 2
    this.ring.position.set(0, 0.012, -0.3)
    g.add(this.ring)
    // An invisible box that catches the pointer.
    this.hit = new THREE.Mesh(shared.boxGeo, new THREE.MeshBasicMaterial({ visible: false }))
    this.hit.scale.set(deskW + 0.4, 2.6, 2.8)
    this.hit.position.set(0, 1.3, -0.4)
    this.hit.userData.pick = pick
    g.add(this.hit)
  }

  setState(state: WorkerState, tip: string) {
    this.state = state
    const c = STATE_COLOR[state]
    this.bulbMat.emissive.setHex(c)
    this.stripeMat.emissive.setHex(c)
    this.ringMat.color.setHex(c)
    this.bodyMat.color.setHSL(...this.bodyHsl(state))
    this.hit.userData.tip = tip
  }

  private baseHsl: [number, number, number] | null = null
  private bodyHsl(state: WorkerState): [number, number, number] {
    if (!this.baseHsl) {
      const hsl = { h: 0, s: 0, l: 0 }
      this.bodyMat.color.getHSL(hsl)
      this.baseHsl = [hsl.h, hsl.s, hsl.l]
    }
    const [h, s, l] = this.baseHsl
    return state === 'off' ? [h, s * 0.15, l * 0.7] : [h, s, l]
  }

  setLabel(lines: Line[], dark: boolean) {
    const key = JSON.stringify([lines, dark])
    if (key === this.labelKey) return
    this.labelKey = key
    if (this.label) {
      this.group.remove(this.label)
      disposeSprite(this.label)
    }
    this.label = makeLabel(lines, {
      bg: dark ? 'rgba(24,26,32,0.86)' : 'rgba(255,255,255,0.9)',
      border: dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)',
      maxWidth: 180,
    })
    this.label.position.set(0, LABEL_Y, -0.4)
    this.label.center.set(0.5, 0)
    this.group.add(this.label)
    this.stack()
  }

  /** Task cards go on top of the label, a few pixels apart, whatever the camera distance. */
  private stack() {
    let offset = (this.label ? labelSize(this.label)[1] : 0) + 5
    for (const card of this.cards) {
      const h = labelSize(card)[1] || 1
      card.center.set(0.5, -offset / h)
      offset += h + 4
    }
  }

  setCards(tasks: TaskView[], dark: boolean, pickables: THREE.Object3D[], labels: OfficeLabels) {
    const shown = tasks.slice(0, MAX_CARDS)
    const extra = tasks.length > shown.length ? labels.moreTasks(tasks.length - shown.length) : ''
    const key = JSON.stringify([shown, tasks.length, dark, extra])
    if (key === this.cardsKey) return
    this.cardsKey = key
    for (const card of this.cards) {
      this.group.remove(card)
      drop(pickables, card)
      disposeSprite(card)
    }
    this.cards = shown.map((task, i) => {
      const lines: Line[] = [{ text: task.goal || task.id, color: '#212529', bold: true, size: 13 }]
      if (task.step) lines.push({ text: task.step, color: '#495057', size: 12 })
      if (i === shown.length - 1 && tasks.length > shown.length)
        lines.push({ text: extra, color: '#868e96', size: 12 })
      const card = makeLabel(lines, { bg: '#fff3bf', border: '#fab005', maxWidth: 180, radius: 6, pad: 7 })
      card.position.set(0, LABEL_Y, -0.4)
      card.userData.pick = { kind: 'task', id: task.id } satisfies Pick
      card.userData.tip = `${task.goal}${task.step ? ` — ${task.step}` : ''} ${labels.openTask}`
      this.group.add(card)
      pickables.push(card)
      return card
    })
    this.stack()
  }

  animate(t: number, still: boolean) {
    const p = t + this.phase
    const s = this.state
    if (still) {
      this.robot.rotation.set(s === 'off' ? 0.3 : 0, 0, 0)
      return
    }
    if (s === 'busy') {
      this.head.position.y = 1.52 + Math.sin(p * 7) * 0.025
      this.robot.rotation.set(0.06 + Math.sin(p * 3.5) * 0.03, Math.sin(p * 1.3) * 0.12, 0)
      this.stripeMat.emissiveIntensity = 0.7 + 0.5 * Math.abs(Math.sin(p * 9))
      this.ringMat.opacity = 0.55 + 0.3 * Math.sin(p * 4)
      this.bulbMat.emissiveIntensity = 1.6
    } else if (s === 'starting') {
      this.robot.rotation.set(0, Math.sin(p * 2) * 0.35, 0)
      this.bulbMat.emissiveIntensity = Math.sin(p * 6) > 0 ? 2.2 : 0.3
      this.ringMat.opacity = 0.4 + 0.4 * Math.abs(Math.sin(p * 2.5))
      this.stripeMat.emissiveIntensity = 0.8
    } else if (s === 'error') {
      this.robot.rotation.set(0.15, 0, Math.sin(p * 8) * 0.05)
      this.bulbMat.emissiveIntensity = Math.sin(p * 10) > 0 ? 2.4 : 0.2
      this.ringMat.opacity = 0.8
    } else if (s === 'off') {
      this.robot.rotation.set(0.32, 0, 0.08)
      this.head.position.y = 1.5
      this.bulbMat.emissiveIntensity = 0.15
      this.stripeMat.emissiveIntensity = 0.15
      this.ringMat.opacity = 0.25
    } else {
      // Idle: a still pose. Only real work (or a worker starting or failing) moves in the office.
      this.head.position.y = 1.52
      this.robot.rotation.set(0, 0, 0)
      this.bulbMat.emissiveIntensity = 1.2
      this.stripeMat.emissiveIntensity = 0.7
      this.ringMat.opacity = 0.7
    }
  }

  /** Whether this desk moves on its own: busy, starting or failing. */
  get moving(): boolean {
    return this.state === 'busy' || this.state === 'starting' || this.state === 'error'
  }

  setSleeping(on: boolean) {
    if (on && !this.zzz) {
      this.zzz = makeLabel([{ text: 'z z z', color: '#adb5bd', bold: true, size: 16 }], { pad: 2 })
      this.zzz.position.set(0.55, 1.95, -0.95)
      this.group.add(this.zzz)
    } else if (!on && this.zzz) {
      this.group.remove(this.zzz)
      disposeSprite(this.zzz)
      this.zzz = null
    }
  }
}

type Beam = { line: THREE.Line; token: THREE.Mesh; curve: THREE.QuadraticBezierCurve3; offset: number }
type Packet = { mesh: THREE.Mesh; curve: THREE.QuadraticBezierCurve3; start: number; duration: number }

export function webglAvailable(): boolean {
  try {
    const canvas = document.createElement('canvas')
    return !!(canvas.getContext('webgl2') || canvas.getContext('webgl'))
  } catch {
    return false
  }
}

export class OfficeScene {
  private renderer: THREE.WebGLRenderer
  private scene = new THREE.Scene()
  private camera = new THREE.PerspectiveCamera(38, 1, 0.1, 200)
  private controls: OrbitControls
  private shared: Shared
  private shadows: boolean
  private still: boolean
  private stations = new Map<string, Station>()
  private lead: Station
  private rackLeds: THREE.MeshStandardMaterial[] = []
  private rackPos = new THREE.Vector3(-8, 0, -1.2)
  private rack = new THREE.Group()
  private rackLabel: THREE.Sprite | null = null
  private rackKey = ''
  private queuePos = new THREE.Vector3(6.6, 0, 4.2)
  private queueLabel: THREE.Sprite | null = null
  private queue = new THREE.Group()
  private queueKey = ''
  private labels: OfficeLabels
  private queueHit: THREE.Mesh | null = null
  private gatewayHit: THREE.Mesh | null = null
  private beams = new Map<string, Beam>()
  private beamMat = new THREE.LineDashedMaterial({ color: 0x748ffc, dashSize: 0.25, gapSize: 0.18, transparent: true, opacity: 0.8 })
  private tokenGeo = new THREE.SphereGeometry(0.09, 10, 8)
  private tokenMat = new THREE.MeshBasicMaterial({ color: 0xbac8ff })
  private packets: Packet[] = []
  private packetOk = new THREE.MeshBasicMaterial({ color: 0x63e6be })
  private packetBad = new THREE.MeshBasicMaterial({ color: 0xff6b6b })
  private pickables: THREE.Object3D[] = []
  private floorMat: THREE.MeshStandardMaterial
  private grid: THREE.GridHelper
  private layoutKey = ''
  private raf = 0
  private last = 0
  private start = performance.now()
  private trafficUntil = 0
  private gatewayUp = true
  private observer: ResizeObserver
  private raycaster = new THREE.Raycaster()
  private pointer = new THREE.Vector2()
  private down: { x: number; y: number } | null = null
  private state: OfficeState | null = null
  private wasCompact: boolean | null = null
  private dirty = true // something changed since the last frame was drawn

  constructor(
    private container: HTMLElement,
    private handlers: Handlers,
  ) {
    this.labels = handlers.labels
    const width = container.clientWidth || 800
    this.shadows = width >= 700
    this.still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'low-power' })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75))
    this.renderer.shadowMap.enabled = this.shadows
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap
    this.renderer.domElement.style.display = 'block'
    this.renderer.domElement.style.touchAction = 'none'
    this.renderer.domElement.setAttribute('aria-label', this.labels.aria)
    container.appendChild(this.renderer.domElement)

    this.shared = {
      desk: new THREE.MeshStandardMaterial({ color: 0xc8a47e, roughness: 0.7 }),
      metal: new THREE.MeshStandardMaterial({ color: 0x868e96, roughness: 0.4, metalness: 0.6 }),
      chair: new THREE.MeshStandardMaterial({ color: 0x343a40, roughness: 0.8 }),
      dark: new THREE.MeshStandardMaterial({ color: 0x212529, roughness: 0.5 }),
      eye: new THREE.MeshBasicMaterial({ color: 0x101113 }),
      boxGeo: new THREE.BoxGeometry(1, 1, 1),
    }

    // Light, floor.
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x445066, 1.6))
    const sun = new THREE.DirectionalLight(0xffffff, 1.9)
    sun.position.set(8, 16, 10)
    sun.castShadow = this.shadows
    sun.shadow.mapSize.set(1024, 1024)
    const cam = sun.shadow.camera as THREE.OrthographicCamera
    cam.left = -18
    cam.right = 18
    cam.top = 18
    cam.bottom = -18
    sun.shadow.bias = -0.0008
    this.scene.add(sun)
    this.floorMat = new THREE.MeshStandardMaterial({ color: 0x2b2f38, roughness: 0.95 })
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(120, 120), this.floorMat)
    floor.rotation.x = -Math.PI / 2
    floor.receiveShadow = this.shadows
    this.scene.add(floor)
    // White vertex colours, so the material colour alone sets the line colour for each theme.
    this.grid = new THREE.GridHelper(80, 40, 0xffffff, 0xffffff)
    this.grid.position.y = 0.005
    this.scene.add(this.grid)

    // Lead agent, gateway rack, queue.
    this.lead = new Station({ kind: 'lead', id: 'lead' }, LEAD_COLOR, this.shared, this.shadows, true)
    this.lead.group.position.copy(LEAD_POS)
    this.scene.add(this.lead.group)
    this.pickables.push(this.lead.hit)
    this.buildRack()
    this.queue.position.copy(this.queuePos)
    this.scene.add(this.queue)
    const mat = new THREE.MeshStandardMaterial({ color: 0x364fc7, roughness: 0.9 })
    const rug = new THREE.Mesh(new THREE.CircleGeometry(1.0, 40), mat)
    rug.rotation.x = -Math.PI / 2
    rug.position.y = 0.01
    this.queue.add(rug)
    const queueHit = new THREE.Mesh(this.shared.boxGeo, new THREE.MeshBasicMaterial({ visible: false }))
    queueHit.scale.set(2.6, 2.5, 2.6)
    queueHit.position.y = 1.2
    queueHit.userData.pick = { kind: 'queue', id: 'queue' } satisfies Pick
    queueHit.userData.tip = this.labels.queueTip
    this.queueHit = queueHit
    this.queue.add(queueHit)
    this.pickables.push(queueHit)

    this.camera.position.set(14, 15, 18)
    this.controls = new OrbitControls(this.camera, this.renderer.domElement)
    this.controls.enableDamping = true
    this.controls.dampingFactor = 0.08
    this.controls.minDistance = 7
    this.controls.maxDistance = 60
    this.controls.maxPolarAngle = Math.PI * 0.44
    this.controls.target.set(0, 0.8, 0)

    const canvas = this.renderer.domElement
    canvas.addEventListener('pointermove', this.onMove)
    canvas.addEventListener('pointerdown', this.onDown)
    canvas.addEventListener('pointerup', this.onUp)
    canvas.addEventListener('pointerleave', this.onLeave)
    this.observer = new ResizeObserver(() => this.resize())
    this.observer.observe(container)
    this.resize()
    this.loop()
  }

  private buildRack() {
    const group = this.rack
    group.position.copy(this.rackPos)
    const body = box(this.shared, 1.2, 2.5, 1.0, this.shared.dark, this.shadows)
    body.position.y = 1.25
    group.add(body)
    for (let i = 0; i < 6; i++) {
      const slot = box(this.shared, 1.0, 0.24, 0.02, this.shared.metal, false)
      slot.position.set(0, 0.45 + i * 0.36, 0.51)
      group.add(slot)
      for (let j = 0; j < 3; j++) {
        const led = new THREE.MeshStandardMaterial({ color: 0x000000, emissive: 0x40c057, emissiveIntensity: 1 })
        const m = box(this.shared, 0.07, 0.07, 0.02, led, false)
        m.position.set(0.25 + j * 0.13, 0.45 + i * 0.36, 0.53)
        group.add(m)
        this.rackLeds.push(led)
      }
    }
    const hit = new THREE.Mesh(this.shared.boxGeo, new THREE.MeshBasicMaterial({ visible: false }))
    hit.scale.set(1.8, 3.2, 1.8)
    hit.position.y = 1.5
    hit.userData.pick = { kind: 'gateway', id: 'gateway' } satisfies Pick
    hit.userData.tip = this.labels.gatewayTip
    this.gatewayHit = hit
    group.add(hit)
    this.pickables.push(hit)
    this.scene.add(group)
  }

  private updateRackLabel(up: boolean, dark: boolean) {
    const statusText = up ? this.labels.gatewayUp : this.labels.gatewayDown
    const key = JSON.stringify([up, dark, statusText])
    if (key === this.rackKey) return
    this.rackKey = key
    if (this.rackLabel) {
      this.rack.remove(this.rackLabel)
      disposeSprite(this.rackLabel)
    }
    this.rackLabel = makeLabel(
      [
        { text: 'Gateway', bold: true, color: dark ? '#f1f3f5' : '#212529' },
        { text: statusText, color: up ? '#20c997' : '#fa5252', size: 13 },
      ],
      { bg: dark ? 'rgba(24,26,32,0.86)' : 'rgba(255,255,255,0.9)', border: dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)' },
    )
    this.rackLabel.position.set(0, 2.75, 0)
    this.rackLabel.center.set(0.5, 0)
    this.rack.add(this.rackLabel)
  }

  /** Where each worker's desk goes: rows behind the lead desk, centred. */
  private layout(ids: string[], compact: boolean): Map<string, THREE.Vector3> {
    const n = ids.length
    const cols = compact ? Math.min(Math.max(n, 1), 2) : n <= 3 ? Math.max(n, 1) : n <= 8 ? 4 : 5
    const spots = new Map<string, THREE.Vector3>()
    ids.forEach((id, i) => {
      const row = Math.floor(i / cols)
      const inRow = Math.min(cols, n - row * cols)
      const col = i % cols
      spots.set(id, new THREE.Vector3((col - (inRow - 1) / 2) * 4.4, 0, -0.6 - row * 4.6))
    })
    return spots
  }

  update(state: OfficeState, labels?: OfficeLabels) {
    if (labels) this.labels = labels
    // Tooltip của 2 hit tạo lúc khởi tạo — làm mới theo ngôn ngữ hiện tại.
    if (this.queueHit) this.queueHit.userData.tip = this.labels.queueTip
    if (this.gatewayHit) this.gatewayHit.userData.tip = this.labels.gatewayTip
    this.state = state
    this.dirty = true
    const dark = state.dark
    const compact = this.compact()
    const background = new THREE.Color(dark ? 0x17191e : 0xeef1f5)
    this.scene.background = background
    this.scene.fog = new THREE.Fog(background, 34, 75)
    this.floorMat.color.setHex(dark ? 0x262a33 : 0xdde2e8)
    ;(this.grid.material as THREE.LineBasicMaterial).color.setHex(dark ? 0x30353f : 0xcfd5dc)
    this.gatewayUp = state.gatewayUp

    // Desks: add, move or remove when the worker list changes.
    const ids = state.workers.map((w) => w.id)
    const key = `${ids.join('|')}${compact ? '#compact' : ''}`
    const relayout = key !== this.layoutKey
    if (relayout) {
      this.layoutKey = key
      for (const [id, station] of this.stations) {
        if (!ids.includes(id)) {
          this.scene.remove(station.group)
          drop(this.pickables, station.hit)
          for (const card of station.cards) drop(this.pickables, card)
          this.disposeTree(station.group)
          this.stations.delete(id)
        }
      }
      const spots = this.layout(ids, compact)
      for (const w of state.workers) {
        let station = this.stations.get(w.id)
        if (!station) {
          station = new Station({ kind: 'worker', id: w.id }, TYPE_COLOR[w.type] ?? 0x74c0fc, this.shared, this.shadows)
          this.stations.set(w.id, station)
          this.scene.add(station.group)
          this.pickables.push(station.hit)
        }
        station.group.position.copy(spots.get(w.id)!)
      }
      // The gateway rack left of the desks, the queue rug right of the lead desk.
      const xs = [...spots.values()].map((v) => v.x)
      this.rackPos.set(Math.min(-2.2, ...xs) - 3.6, 0, -1.2)
      this.queuePos.set(Math.max(4.4, ...xs), 0, 4.2)
      this.rack.position.copy(this.rackPos)
      this.queue.position.copy(this.queuePos)
    }

    const byWorker = new Map<string, TaskView[]>()
    const waiting: TaskView[] = []
    for (const task of state.tasks) {
      if (task.worker && this.stations.has(task.worker)) {
        byWorker.set(task.worker, [...(byWorker.get(task.worker) ?? []), task])
      } else waiting.push(task)
    }
    for (const w of state.workers) {
      const station = this.stations.get(w.id)!
      const tasks = byWorker.get(w.id) ?? []
      const state2: WorkerState = w.state === 'idle' && tasks.length ? 'busy' : w.state
      station.setState(state2, `${w.id} · ${w.stateLabel}${w.model ? ` · ${w.model}` : ''} ${this.labels.workerTip}`)
      station.setSleeping(state2 === 'off')
      const lines: Line[] = [
        { text: w.id, bold: true, color: dark ? '#f1f3f5' : '#212529' },
        {
          text: `${w.stateLabel}${w.running ? this.labels.runningSuffix(w.running, w.slots) : ''}`,
          color: hex((dark ? STATE_COLOR : STATE_TEXT_LIGHT)[state2]),
          size: 13,
        },
      ]
      if (w.model && !compact) lines.push({ text: w.model, color: dark ? '#adb5bd' : '#495057', size: 12 })
      station.setLabel(lines, dark)
      station.setCards(compact ? [] : tasks, dark, this.pickables, this.labels)
    }

    this.lead.setState(state.chatBusy ? 'busy' : 'idle', this.labels.leadTip)
    this.lead.setLabel(
      [
        { text: this.labels.leadName, bold: true, color: dark ? '#f1f3f5' : '#212529' },
        { text: state.chatBusy ? this.labels.answering : this.labels.waiting, color: hex((dark ? STATE_COLOR : STATE_TEXT_LIGHT)[state.chatBusy ? 'busy' : 'idle']), size: 13 },
      ],
      dark,
    )
    this.updateQueue(waiting, dark)
    this.updateRackLabel(state.gatewayUp, dark)
    this.updateBeams([...byWorker.keys()])
    this.sizeLabels()
    if (relayout) this.fit()
  }

  private updateQueue(waiting: TaskView[], dark: boolean) {
    const queueText = waiting.length ? this.labels.queueWaiting(waiting.length) : this.labels.queueEmpty
    const key = JSON.stringify([waiting.map((t) => t.id), dark, this.labels.queueName, queueText])
    if (key === this.queueKey) return
    this.queueKey = key
    for (const child of [...this.queue.children]) {
      if (child.userData.queueItem) {
        this.queue.remove(child)
        if (child instanceof THREE.Sprite) disposeSprite(child)
        else if (child instanceof THREE.Mesh) (child.material as THREE.Material).dispose()
      }
    }
    const colors = [0xffe066, 0xffd43b, 0xfcc419]
    waiting.slice(0, 10).forEach((_, i) => {
      const paper = box(this.shared, 0.7, 0.1, 0.5, new THREE.MeshStandardMaterial({ color: colors[i % 3] }), this.shadows)
      paper.position.set(Math.sin(i * 1.7) * 0.06, 0.08 + i * 0.11, Math.cos(i * 1.3) * 0.06)
      paper.rotation.y = i * 0.35
      paper.userData.queueItem = true
      this.queue.add(paper)
    })
    const label = makeLabel(
      [
        { text: this.labels.queueName, bold: true, color: dark ? '#f1f3f5' : '#212529' },
        { text: queueText, color: dark ? '#adb5bd' : '#495057', size: 13 },
      ],
      { bg: dark ? 'rgba(24,26,32,0.86)' : 'rgba(255,255,255,0.9)', border: dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)' },
    )
    this.queueLabel = label
    label.position.set(0, 0.25 + Math.min(waiting.length, 10) * 0.11, 0)
    label.center.set(0.5, -0.25)
    label.userData.queueItem = true
    this.queue.add(label)
  }

  private updateBeams(workers: string[]) {
    for (const [id, beam] of this.beams) {
      if (!workers.includes(id) || !this.stations.has(id)) {
        this.scene.remove(beam.line, beam.token)
        beam.line.geometry.dispose()
        this.beams.delete(id)
      }
    }
    for (const id of workers) {
      const to = this.stations.get(id)!.group.position.clone().setY(2.0)
      const from = LEAD_POS.clone().setY(2.0)
      const existing = this.beams.get(id)
      if (existing && existing.curve.v2.distanceTo(to) < 0.01) continue
      if (existing) {
        this.scene.remove(existing.line, existing.token)
        existing.line.geometry.dispose()
      }
      const mid = from.clone().add(to).multiplyScalar(0.5).setY(4.2)
      const curve = new THREE.QuadraticBezierCurve3(from, mid, to)
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(40)), this.beamMat)
      line.computeLineDistances()
      const token = new THREE.Mesh(this.tokenGeo, this.tokenMat)
      this.scene.add(line, token)
      this.beams.set(id, { line, token, curve, offset: Math.random() })
    }
  }

  /** A model request answered by a worker: a dot flies to its desk (from the lead desk for the chat). */
  pulse(worker: string, fromChat: boolean, ok: boolean) {
    const station = this.stations.get(worker)
    if (!station || this.packets.length > 40) return
    const from = (fromChat ? LEAD_POS.clone() : this.rackPos.clone()).setY(fromChat ? 2.0 : 2.6)
    const to = station.group.position.clone().setY(1.9)
    const mid = from.clone().add(to).multiplyScalar(0.5).setY(5)
    const meshObj = new THREE.Mesh(this.tokenGeo, ok ? this.packetOk : this.packetBad)
    meshObj.scale.setScalar(1.4)
    this.scene.add(meshObj)
    this.packets.push({ mesh: meshObj, curve: new THREE.QuadraticBezierCurve3(from, mid, to), start: performance.now(), duration: 1100 })
    this.trafficUntil = performance.now() + 1500
  }

  /** Point the camera at the whole office, labels included, from as close as it can, and centre it. */
  private fit() {
    type Spot = { p: THREE.Vector3; up: number; half: number } // world point, label pixels above it, half width
    const k = this.compact() ? 0.82 : 1
    const spots: Spot[] = []
    const floor = (c: THREE.Vector3, w: number, d: number) => {
      for (const dx of [-w, w]) for (const dz of [-d, d]) spots.push({ p: new THREE.Vector3(c.x + dx, 0, c.z + dz), up: 0, half: 0 })
    }
    const labels = (at: THREE.Vector3, sprites: (THREE.Sprite | null)[], lift = 0) => {
      let up = lift
      let half = 0
      for (const sprite of sprites) {
        if (!sprite) continue
        const [w, h] = labelSize(sprite).map((v) => v * k)
        up += h + 5
        half = Math.max(half, w / 2)
      }
      if (up) spots.push({ p: at, up, half })
    }
    for (const s of [this.lead, ...this.stations.values()]) {
      floor(s.group.position, 1.3, 1.0)
      labels(s.group.position.clone().add(new THREE.Vector3(0, LABEL_Y, -0.4)), [s.label, ...s.cards])
    }
    floor(this.rackPos, 0.8, 0.7)
    labels(this.rackPos.clone().setY(2.75), [this.rackLabel])
    floor(this.queuePos, 1.0, 1.0)
    if (this.queueLabel) {
      const lift = labelSize(this.queueLabel)[1] * 0.25
      labels(this.queuePos.clone().add(this.queueLabel.position), [this.queueLabel], lift)
    }

    const width = this.container.clientWidth || 800
    const height = this.container.clientHeight || 480
    const target = new THREE.Box3().setFromPoints(spots.map((s) => s.p)).getCenter(new THREE.Vector3()).setY(0.6)
    const dir = new THREE.Vector3(0.16, 0.8, 1).normalize()
    const place = (distance: number) => {
      this.camera.position.copy(target).addScaledVector(dir, distance)
      this.camera.lookAt(target)
      this.camera.updateMatrixWorld()
    }
    // What the camera sees of it, in screen coordinates from -1 to 1.
    const rect = () => {
      let x0 = Infinity
      let x1 = -Infinity
      let y0 = Infinity
      let y1 = -Infinity
      for (const s of spots) {
        const v = s.p.clone().project(this.camera)
        x0 = Math.min(x0, v.x - (2 * s.half) / width)
        x1 = Math.max(x1, v.x + (2 * s.half) / width)
        y0 = Math.min(y0, v.y)
        y1 = Math.max(y1, v.y + (2 * s.up) / height)
      }
      return { x0, x1, y0, y1 }
    }
    let distance = 9
    for (; distance < 60; distance += 0.5) {
      place(distance)
      const r = rect()
      if (r.x1 - r.x0 < 1.86 && r.y1 - r.y0 < 1.84) break
    }
    // Slide the camera (and the point it turns around) so the picture sits in the middle.
    const tanV = Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))
    for (let i = 0; i < 3; i++) {
      const r = rect()
      const right = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 0)
      const up = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 1)
      target
        .addScaledVector(right, ((r.x0 + r.x1) / 2) * distance * tanV * this.camera.aspect)
        .addScaledVector(up, ((r.y0 + r.y1) / 2) * distance * tanV)
      place(distance)
    }
    this.controls.target.copy(target)
    this.controls.update()
  }

  /** One screen pixel as a sprite scale, for labels that keep their size (see labels.ts). */
  private sizeLabels() {
    const height = this.container.clientHeight || 480
    const unit = ((2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) / height) * (this.compact() ? 0.82 : 1)
    this.scene.traverse((obj) => {
      if (obj instanceof THREE.Sprite) sizeLabel(obj, unit)
    })
  }

  private resize() {
    const w = this.container.clientWidth
    const h = this.container.clientHeight
    if (!w || !h) return
    const first = this.camera.aspect === 1
    const wasCompact = this.wasCompact
    this.renderer.setSize(w, h)
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
    this.wasCompact = this.compact()
    this.dirty = true
    if (this.state && wasCompact !== this.wasCompact) this.update(this.state)
    if (first && this.layoutKey) this.fit()
    this.sizeLabels()
  }

  private compact(): boolean {
    return (this.container.clientWidth || 800) < COMPACT_WIDTH
  }

  private loop = () => {
    this.raf = requestAnimationFrame(this.loop)
    if (document.hidden) return
    const now = performance.now()
    if (now - this.last < 1000 / 45) return
    this.last = now
    const t = (now - this.start) / 1000
    const turning = this.controls.update()
    this.lead.animate(t, this.still)
    for (const s of this.stations.values()) s.animate(t, this.still)
    // Nothing happening and nothing changed: skip drawing, so an idle office costs no CPU or GPU.
    const traffic = now < this.trafficUntil
    const live =
      !this.still &&
      (this.lead.moving || [...this.stations.values()].some((s) => s.moving) || this.beams.size > 0 || traffic || !this.gatewayUp)
    if (!turning && !live && !this.packets.length && !this.dirty) return
    this.dirty = false
    for (const beam of this.beams.values()) {
      const k = (t * 0.45 + beam.offset) % 1
      beam.token.position.copy(beam.curve.getPoint(k))
    }
    this.packets = this.packets.filter((p) => {
      const k = (now - p.start) / p.duration
      if (k >= 1) {
        this.scene.remove(p.mesh)
        return false
      }
      p.mesh.position.copy(p.curve.getPoint(k))
      return true
    })
    const busy = traffic
    this.rackLeds.forEach((led, i) => {
      led.emissive.setHex(this.gatewayUp ? 0x40c057 : 0xfa5252)
      led.emissiveIntensity = !this.gatewayUp
        ? (Math.sin(t * 4) > 0 ? 1.6 : 0.2)
        : busy && !this.still
          ? (Math.sin(t * 23 + i * 2.1) > 0.2 ? 1.8 : 0.25)
          : 0.9
    })
    this.renderer.render(this.scene, this.camera)
  }

  private pickAt(event: PointerEvent): THREE.Object3D | null {
    const rect = this.renderer.domElement.getBoundingClientRect()
    this.pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1)
    this.raycaster.setFromCamera(this.pointer, this.camera)
    const hit = this.raycaster.intersectObjects(this.pickables, false)[0]
    return hit?.object ?? null
  }

  private onMove = (event: PointerEvent) => {
    if (event.buttons) return
    const object = this.pickAt(event)
    this.renderer.domElement.style.cursor = object ? 'pointer' : 'grab'
    this.handlers.onHover(object?.userData.tip ?? null, event.clientX, event.clientY)
  }

  private onDown = (event: PointerEvent) => {
    this.down = { x: event.clientX, y: event.clientY }
  }

  private onUp = (event: PointerEvent) => {
    if (!this.down) return
    const moved = Math.hypot(event.clientX - this.down.x, event.clientY - this.down.y)
    this.down = null
    if (moved > 6) return
    const object = this.pickAt(event)
    if (object?.userData.pick) this.handlers.onPick(object.userData.pick as Pick)
  }

  private onLeave = () => this.handlers.onHover(null, 0, 0)

  private disposeTree(root: THREE.Object3D) {
    root.traverse((o) => {
      if (o instanceof THREE.Sprite) disposeSprite(o)
      else if (o instanceof THREE.Mesh) {
        if (o.geometry !== this.shared.boxGeo && o.geometry !== this.tokenGeo) o.geometry.dispose()
        const mats = Array.isArray(o.material) ? o.material : [o.material]
        for (const m of mats) if (!Object.values(this.shared).includes(m)) m.dispose()
      }
    })
  }

  dispose() {
    cancelAnimationFrame(this.raf)
    this.observer.disconnect()
    this.controls.dispose()
    const canvas = this.renderer.domElement
    canvas.removeEventListener('pointermove', this.onMove)
    canvas.removeEventListener('pointerdown', this.onDown)
    canvas.removeEventListener('pointerup', this.onUp)
    canvas.removeEventListener('pointerleave', this.onLeave)
    this.disposeTree(this.scene)
    for (const value of Object.values(this.shared)) value.dispose()
    this.beamMat.dispose()
    this.tokenGeo.dispose()
    this.tokenMat.dispose()
    this.packetOk.dispose()
    this.packetBad.dispose()
    this.renderer.dispose()
    canvas.remove()
    this.state = null
  }

  get current(): OfficeState | null {
    return this.state
  }
}

function drop<T>(list: T[], item: T) {
  const i = list.indexOf(item)
  if (i >= 0) list.splice(i, 1)
}

function hex(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`
}
