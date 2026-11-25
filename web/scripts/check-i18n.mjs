// check-i18n.mjs — Cổng kiểm i18n + sáng/tối cho Gói 0(b) (Kite UI).
//
// Chạy:  node web/scripts/check-i18n.mjs [--json]
//        (chạy từ bất cứ đâu; script tự tìm thư mục web/ theo vị trí của nó)
// Thoát 0 = không lỗi; thoát 1 = còn lỗi (xem báo cáo).
//
// Kiểm 3 thứ (theo hợp đồng Gói 0(b) trong HOP-DONG-GIAO-TIEP.md):
//   (1) Chuỗi tiếng Việt hard-code còn sót trong text UI ở
//       web/src/pages/, web/src/components/, web/src/App.tsx
//       (trừ web/src/i18n/ — đó là nơi chuỗi được phép ở).
//       Quét cả string literal ('...', "...", `...`) lẫn text JSX thuần
//       (<Text>Đang tải...</Text>). Comment đã loại trước khi quét.
//   (2) Mọi key dùng trong t('...') đều có trong CẢ HAI từ điển
//       web/src/i18n/vi.ts và web/src/i18n/en.ts (đối chiếu tĩnh bằng
//       grep/phân tích cú pháp — parity kiểu TypeScript chỉ bắt thiếu
//       key ở phía định nghĩa, không bắt key dùng mà không định nghĩa).
//       Kèm kiểm parity hai chiều vi<->en và cấm key động
//       (t('tasks.' + x) / t(`tasks.${x}`) — key phải greppable tĩnh).
//       NGOẠI LỆ DUY NHẤT cho key động: key tra từ cấu trúc tĩnh có thể
//       liệt kê — bốn dạng, tất cả đều phải là DictKey (TypeScript bắt):
//         (1) t(var) với var gán từ bảng `Record<..., DictKey>`
//             (vd TASK_STATE_KEYS[state] trong common.tsx);
//         (2) t(BẢNG[expr]) — tra bảng inline
//             (vd t(WORKER_STATE_KEYS[key]), t(PHASES[a.phase]));
//         (3) t(obj.prop) với prop khai báo kiểu DictKey trong type của
//             file (vd FieldDef.labelKey/descKey, giá trị gán trong bảng
//             FIELDS). Script liệt kê riêng các tra này ở mục "warning"
//             (không tính lỗi) VÀ kiểm mọi giá trị key trong các bảng/
//             descriptor đó đều có trong cả hai từ điển (thiếu thì vẫn
//             là lỗi). Mọi dạng key động khác (nối chuỗi, biến tự do,
//             key từ dữ liệu backend) vẫn là LỖI.
//         (4) t(key) với key tách ra từ Object.entries(BẢNG).map(
//             ([_, key]) => ...) — BẢNG là Record<..., DictKey> tĩnh
//             (vd FILE_KEYS trong Journal.tsx); giá trị key trong BẢNG
//             được kiểm có trong cả hai từ điển như các bảng khác.
//   (3) app.css có override chế độ sáng cho các selector bắt buộc:
//       .md pre, .md code, .diff, .screen, .bubble-user.
//       Một selector được miễn khi app.css có comment đánh dấu
//       `light-ok: <selector> — <lý do>` (vd .screen là mặt màn hình
//       trình duyệt từ xa, nền đen là đúng cả hai chế độ — lý do phải
//       ghi rõ trong comment để người review kiểm lại được).
//
// Ngoại lệ KHÔNG qua t() (theo hợp đồng) — script phân biệt như sau:
//   - Dữ liệu backend (tên task/goal do chủ gõ, tên repo, log): đó là GIÁ
//     TRỊ BIẾN ({task.title}, {repo.name}...), không phải string literal
//     nên script vốn đã không quét chúng. Chỉ literal mới bị quét.
//   - Danh từ riêng / giá trị literal tiếng Việt hợp lệ khác: ghi vào
//     ALLOWLIST dưới đây (mỗi mục kèm lý do) — script liệt kê các mục
//     allowlist đã áp dụng trong báo cáo để người đọc kiểm lại.
//   - Dòng có comment `i18n-ignore`: bỏ qua finding trên đúng dòng đó
//     (dùng khi ngoại lệ chỉ xuất hiện một chỗ, kèm lý do trong comment).
//   - console.log/warn/error: không phải text UI — liệt kê riêng ở mục
//     "info", KHÔNG tính là lỗi.
//
// Không dependency. Node >= 20.
import { readdir, readFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SRC = path.join(WEB_ROOT, 'src')
const I18N_DIR = path.join(SRC, 'i18n')

// Phạm vi quét chuỗi hard-code (theo hợp đồng): đúng 3 chỗ này.
const SCAN_DIRS = [path.join(SRC, 'pages'), path.join(SRC, 'components')]
const SCAN_FILES = [path.join(SRC, 'App.tsx')]
// Phạm vi quét cách dùng t(): toàn bộ src/ trừ i18n/ (key có thể dùng ở
// bất cứ file UI nào, kể cả office/ hay util.ts).
const USAGE_SCAN_EXCLUDE = [I18N_DIR]

// Ký tự tiếng Việt có dấu (chữ cái + đ). Chuỗi chỉ có chữ không dấu
// ("Kite", "Kaggle", "VI | EN") không bị tính là tiếng Việt.
const VI_RE =
  /[àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵÀÁẢÃẠĂẮẰẲẴẶÂẤẦẨẪẬĐÈÉẺẼẸÊẾỀỂỄỆÌÍỈĨỊÒÓỎÕỌÔỐỒỔỖỘƠỚỜỞỠỢÙÚỦŨỤƯỨỪỬỮỰỲÝỶỸỴ]/

// Một đoạn key: kebab-case, bắt đầu bằng chữ — hoặc bắt đầu bằng số nhưng
// phải chứa ít nhất một chữ (vd '7d', '30d' trong journal.range.7d);
// đoạn chỉ toàn số ('7', '30') vẫn sai định dạng.
// Key i18n hợp lệ: namespace.key, vd tasks.detail.apply-patch, journal.range.7d
const SEGMENT = '(?:[a-z][a-z0-9]*(?:-[a-z0-9]+)*|[0-9]+[a-z][a-z0-9]*(?:-[a-z0-9]+)*)'
const KEY_RE = new RegExp(`^${SEGMENT}(?:\\.${SEGMENT})+$`)
// Mẫu chuỗi trích dẫn khớp dạng key (gom key đã định nghĩa / giá trị bảng
// tra). Dùng cờ 'i' để giữ độ bao phủ cũ (từng chấp nhận cả chữ hoa).
const QUOTED_KEY_RE = () => new RegExp(`['"\`](${SEGMENT}(?:\\.${SEGMENT})+)['"\`]`, 'gi')

// Ngoại lệ literal hợp lệ (danh từ riêng / giá trị backend viết cứng).
// Mỗi mục phải kèm lý do trong comment; hiện để trống vì chưa phát hiện
// ngoại lệ hợp lệ nào — worker (a) bổ sung khi gặp, kèm lý do rõ ràng.
// (Ví dụ mẫu — KHÔNG bật: 'Huỳnh Ngọc Thuận' // tên riêng của chủ.)
const ALLOWLIST = new Set([
  // (trống)
])

// Selector bắt buộc phải có override chế độ sáng trong app.css.
const LIGHT_REQUIRED = ['.md pre', '.md code', '.diff', '.screen', '.bubble-user']
const LIGHT_ATTR = "[data-mantine-color-scheme='light']"

// ---------------------------------------------------------------------------
// Thu thập file .ts/.tsx (đệ quy), trừ thư mục loại khỏi phạm vi.
// ---------------------------------------------------------------------------
async function collectFiles(dirs, excludeDirs = []) {
  const out = []
  async function walk(dir) {
    let entries
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      const full = path.join(dir, e.name)
      if (excludeDirs.some((x) => full === x || full.startsWith(x + path.sep))) continue
      if (e.isDirectory()) await walk(full)
      else if (/\.(tsx?|mts|cts)$/.test(e.name)) out.push(full)
    }
  }
  for (const d of dirs) await walk(d)
  return out.sort()
}

// ---------------------------------------------------------------------------
// Tách chuỗi literal + comment khỏi code.
// Trả về:
//   strings: [{ line, col, text }] — nội dung từng literal (template literal:
//              từng đoạn ngoài ${...} là một mục riêng).
//   blanked: string — code gốc nhưng nội dung literal/comment đã thay bằng
//            khoảng trắng (giữ nguyên xuống dòng để số dòng không lệch),
//            dùng để quét text JSX và cách dùng t().
// ---------------------------------------------------------------------------
function splitStringsAndComments(source) {
  const strings = []
  const out = source.split('')
  const n = source.length
  let i = 0
  let line = 1
  // Ngăn xếp trạng thái: 'code' | 'sq' | 'dq' | 'tpl' | 'line-comment' | 'block-comment'
  // Template literal có thể chứa ${ ... code ... } — theo dõi độ sâu ngoặc
  // nhọn trong ${} để biết khi nào quay lại phần literal.
  const stack = [{ kind: 'code' }]
  let cur = null // literal đang mở: { quote, line, col, buf, tplDepth }
  const colOf = (idx) => idx - source.lastIndexOf('\n', idx - 1)

  function blank(from, to) {
    for (let k = from; k < to; k++) if (out[k] !== '\n') out[k] = ' '
  }
  function top() {
    return stack[stack.length - 1]
  }
  function pushString(quote, startIdx) {
    cur = { quote, line, col: colOf(startIdx), buf: '', startIdx }
    stack.push({ kind: quote === "'" ? 'sq' : quote === '"' ? 'dq' : 'tpl' })
  }
  function endString(endIdx) {
    if (cur && cur.buf.length > 0) strings.push({ line: cur.line, col: cur.col, text: cur.buf })
    if (cur) blank(cur.startIdx, endIdx)
    cur = null
    stack.pop()
  }

  while (i < n) {
    const ch = source[i]
    const nxt = source[i + 1]
    const st = top()
    if (ch === '\n') line++

    if (st.kind === 'line-comment') {
      if (ch === '\n') stack.pop()
      i++
      continue
    }
    if (st.kind === 'block-comment') {
      if (ch === '*' && nxt === '/') {
        blank(stack[stack.length - 1].start, i + 2)
        stack.pop()
        i += 2
        continue
      }
      i++
      continue
    }
    if (st.kind === 'sq' || st.kind === 'dq') {
      const q = st.kind === 'sq' ? "'" : '"'
      if (ch === '\\') {
        cur.buf += source.slice(i, i + 2)
        i += 2
        continue
      }
      if (ch === q) {
        endString(i + 1)
        i++
        continue
      }
      if (ch === '\n') {
        // Chuỗi '...' xuống dòng không hợp lệ — coi như kết thúc để khỏi lệch.
        endString(i)
        i++
        continue
      }
      cur.buf += ch
      i++
      continue
    }
    if (st.kind === 'tpl') {
      if (ch === '\\') {
        cur.buf += source.slice(i, i + 2)
        i += 2
        continue
      }
      if (ch === '`') {
        endString(i + 1)
        i++
        continue
      }
      if (ch === '$' && nxt === '{') {
        // Kết thúc đoạn literal, vào code trong ${...}: tạm gỡ trạng
        // thái 'tpl' khỏi ngăn xếp (khi '}' đóng sẽ đẩy lại).
        if (cur.buf.length > 0) strings.push({ line: cur.line, col: cur.col, text: cur.buf })
        blank(cur.startIdx, i)
        cur = null
        stack.pop()
        stack.push({ kind: 'tpl-expr', depth: 0 })
        i += 2
        continue
      }
      cur.buf += ch
      i++
      continue
    }
    if (st.kind === 'tpl-expr') {
      if (ch === '{') st.depth++
      else if (ch === '}') {
        if (st.depth === 0) {
          stack.pop()
          // Quay lại phần literal của template, mở literal mới.
          cur = { quote: '`', line, col: colOf(i + 1), buf: '', startIdx: i + 1 }
          stack.push({ kind: 'tpl' })
          i++
          continue
        }
        st.depth--
      } else if (ch === "'" || ch === '"' || ch === '`') {
        pushString(ch, i)
        i++
        continue
      }
      i++
      continue
    }
    // st.kind === 'code'
    if (ch === '/' && nxt === '/') {
      const start = i
      stack.push({ kind: 'line-comment', start })
      i += 2
      continue
    }
    if (ch === '/' && nxt === '*') {
      stack.push({ kind: 'block-comment', start: i })
      i += 2
      continue
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      pushString(ch, i)
      i++
      continue
    }
    i++
  }
  // Literal/comment chưa đóng ở cuối file: blank phần còn lại.
  if (cur) blank(cur.startIdx, n)
  const lc = top()
  if (lc.kind === 'line-comment' || lc.kind === 'block-comment') blank(lc.start, n)
  return { strings, blanked: out.join('') }
}

// ---------------------------------------------------------------------------
// Quét text JSX thuần: đoạn text giữa > và < trong code đã blank literal.
// ---------------------------------------------------------------------------
function scanJsxText(blanked, relPath) {
  const findings = []
  const lines = blanked.split('\n')
  // Quét toàn văn (JSX text có thể trải nhiều dòng), rồi quy về dòng bắt đầu.
  const re = />\s*([^<>{}]+?)\s*</g
  let m
  while ((m = re.exec(blanked)) !== null) {
    const text = m[1]
    if (!VI_RE.test(text)) continue
    // Loại đoạn chỉ là ký tự JS vô hại lọt vào (vd ">=" trong code thường —
    // nhưng đoạn đó chứa < > nên regex đã loại phần lớn).
    const trimmed = text.replace(/\s+/g, ' ').trim()
    if (!trimmed) continue
    const line = blanked.slice(0, m.index).split('\n').length
    findings.push({ file: relPath, line, kind: 'jsx-text', text: trimmed })
  }
  void lines
  return findings
}

// ---------------------------------------------------------------------------
// Trích key dùng trong t(...) từ code đã blank literal/comment.
// Trả về { used: Map<key, {file, line}[]>, dynamic: [{file, line, snippet}] }
// ---------------------------------------------------------------------------
function extractTUsages(blanked, relPath, rawLines) {
  const used = new Map()
  const dynamic = []
  const tableLookups = []
  const sourceText = rawLines.join('\n')
  // Bảng tra key tĩnh: const X: Record<..., DictKey> = { ... }
  const tableNames = new Set()
  for (const m of sourceText.matchAll(/const\s+([A-Za-z_$][\w$]*)\s*:\s*Record<[^>]*DictKey[^>]*>\s*=\s*\{/g)) {
    tableNames.add(m[1])
  }
  // Thuộc tính khai báo kiểu DictKey trong type/interface của file
  // (vd FieldDef.labelKey: DictKey | null).
  const dictKeyProps = new Set()
  for (const m of sourceText.matchAll(/([A-Za-z_$][\w$]*)\??:\s*DictKey\b/g)) {
    dictKeyProps.add(m[1])
  }
  // Không dùng \b: \b của JS chỉ hiểu ASCII nên chữ có dấu (vd 'nhất')
  // tạo ranh giới từ giả và 't (' cuối từ tiếng Việt bị nhận nhầm là t().
  // Lookbehind loại ký tự định danh (kể cả chữ Latin mở rộng).
  // Lưu ý: blanked đã thay nội dung literal bằng khoảng trắng nên key
  // phải đọc từ dòng gốc (rawLines) tại đúng vị trí t(.
  // Dạng (4): key tách ra từ Object.entries(BẢNG).map(([_, key]) => ...)
  // với BẢNG là bảng tra tĩnh Record<..., DictKey>.
  const entryKeyIds = new Map()
  for (const m of sourceText.matchAll(
    /Object\.entries\(\s*([A-Za-z_$][\w$]*)\s*\)\s*\.map\(\s*\(\s*\[\s*[A-Za-z_$][\w$]*\s*,\s*([A-Za-z_$][\w$]*)\s*\]/g,
  )) {
    if (tableNames.has(m[1])) entryKeyIds.set(m[2], m[1])
  }
  const re = /(?<![A-Za-z0-9_$\u00C0-\u024F\u1E00-\u1EFF])t\s*\(/g
  let m
  while ((m = re.exec(blanked)) !== null) {
    const line = blanked.slice(0, m.index).split('\n').length
    const raw = rawLines[line - 1] ?? ''
    const snippet = raw.trim().slice(0, 120)
    const lineStart = blanked.lastIndexOf('\n', m.index - 1) + 1
    const colInLine = m.index - lineStart
    const fromCol = raw.indexOf('t', colInLine)
    if (fromCol === -1) continue
    const keyMatch = /^t\s*\(\s*(['"`])((?:\\.|(?!\1)[\s\S])*?)\1/.exec(raw.slice(fromCol))
    if (!keyMatch) {
      const rest = raw.slice(fromCol)
      // Dạng (2): t(BẢNG[expr]) — tra bảng inline.
      const inlineMatch = /^t\s*\(\s*([A-Za-z_$][\w$]*)\s*\[/.exec(rest)
      if (inlineMatch && tableNames.has(inlineMatch[1])) {
        tableLookups.push({ file: relPath, line, snippet, variable: `${inlineMatch[1]}[...]`, table: inlineMatch[1] })
        continue
      }
      // Dạng (3): t(obj.prop) với prop khai báo kiểu DictKey.
      const propMatch = /^t\s*\(\s*[A-Za-z_$][\w$]*\.([A-Za-z_$][\w$]*)\s*\)/.exec(rest)
      if (propMatch && dictKeyProps.has(propMatch[1])) {
        tableLookups.push({ file: relPath, line, snippet, variable: `*.${propMatch[1]}`, table: `descriptor prop ${propMatch[1]}` })
        continue
      }
      // Dạng (1): t(biến) — kiểm tra biến có được gán từ bảng tra tĩnh không.
      const idMatch = /^t\s*\(\s*([A-Za-z_$][\w$]*)\s*\)/.exec(raw.slice(fromCol))
      if (idMatch && tableNames.size > 0) {
        const id = idMatch[1]
        // Dạng (4): biến tách ra từ Object.entries(BẢNG).map(([_, key]) => ...).
        if (entryKeyIds.has(id)) {
          tableLookups.push({ file: relPath, line, snippet, variable: id, table: entryKeyIds.get(id) })
          continue
        }
        const tableAlt = [...tableNames].join('|')
        const assignRe = new RegExp(`(?:const|let|var)\\s+${id}\\s*=\\s*(${tableAlt})\\s*\\[`)
        const tableHit = assignRe.exec(sourceText)
        if (tableHit) {
          tableLookups.push({ file: relPath, line, snippet, variable: id, table: tableHit[1] })
          continue
        }
      }
      dynamic.push({ file: relPath, line, snippet, reason: 'đối số đầu của t() không phải string literal (key động)' })
      continue
    }
    const [, quote, key] = keyMatch
    if (quote === '`' && key.includes('${')) {
      dynamic.push({ file: relPath, line, snippet, reason: 'key template có ${...} (key động)' })
      continue
    }
    // Sau quote đóng phải là ')' hoặc ',' — nếu là '+' thì đây là nối
    // chuỗi (key động), không phải key tĩnh.
    const after = raw.slice(fromCol + keyMatch[0].length).trimStart()
    if (after !== '' && after[0] !== ')' && after[0] !== ',') {
      dynamic.push({ file: relPath, line, snippet, reason: `sau key '${key}' còn '${after.slice(0, 20)}' — key bị nối/chế biến (key động)` })
      continue
    }
    if (!KEY_RE.test(key)) {
      dynamic.push({ file: relPath, line, snippet, reason: `key '${key}' sai định dạng namespace.key` })
      continue
    }
    if (!used.has(key)) used.set(key, [])
    used.get(key).push({ file: relPath, line })
  }
  return { used, dynamic, tableLookups }
}

// ---------------------------------------------------------------------------
// Trích mọi giá trị key trong các bảng tra `Record<..., DictKey>` của một
// file (để kiểm chúng cũng có trong cả hai từ điển).
// ---------------------------------------------------------------------------
function extractTableKeyValues(source) {
  const values = new Set()
  const declRe = /:\s*Record<[^>]*DictKey[^>]*>\s*=\s*\{/g
  let dm
  while ((dm = declRe.exec(source)) !== null) {
    let depth = 0
    let i = dm.index + dm[0].length - 1 // vị trí '{'
    const start = i
    for (; i < source.length; i++) {
      if (source[i] === '{') depth++
      else if (source[i] === '}') {
        depth--
        if (depth === 0) break
      }
    }
    const body = source.slice(start, i + 1)
    for (const qm of body.matchAll(QUOTED_KEY_RE())) {
      if (KEY_RE.test(qm[1])) values.add(qm[1])
    }
  }
  return values
}

// ---------------------------------------------------------------------------
// Phân tích từ điển vi.ts / en.ts: gom tập key đã định nghĩa.
// Hỗ trợ cả hai dạng: flat (Record<DictKey, string> + union type) và
// nested object ({ tasks: { detail: { 'apply-patch': '...' } } }).
// ---------------------------------------------------------------------------
function collectDefinedKeys(source) {
  const keys = new Set()
  // Dạng 1: mọi chuỗi trích dẫn khớp mẫu key (bắt được cả union type
  // 'common.save' | 'nav.chat' lẫn key flat trong object).
  const qre = QUOTED_KEY_RE()
  let m
  while ((m = qre.exec(source)) !== null) {
    if (KEY_RE.test(m[1])) keys.add(m[1])
  }
  // Dạng 2: object lồng nhau — theo dõi ngăn xếp key qua tokenizer thô.
  // Quy tắc: '{' chỉ nhận key đang chờ khi ngay trước nó là ':' (giá trị
  // của key đó là object); các '{' khác (vd sau '=' hay sau kiểu
  // Record<...>) đẩy null để khỏi gán nhầm key của biểu thức bên ngoài.
  const tre = /([A-Za-z_$][\w$]*)|(['"])((?:\\.|(?!\2)[\s\S])*?)\2|([{}:,])/g
  const stack = []
  let pendingKey = null
  let lastToken = null // '{' | '}' | ':' | ',' | 'str' | 'ident' | null
  while ((m = tre.exec(source)) !== null) {
    const [, ident, q, str, punct] = m
    if (punct === '{') {
      if (pendingKey !== null && lastToken === ':') stack.push(pendingKey)
      else stack.push(null)
      pendingKey = null
      lastToken = '{'
    } else if (punct === '}') {
      stack.pop()
      pendingKey = null
      lastToken = '}'
    } else if (punct === ':') {
      lastToken = ':'
    } else if (punct === ',') {
      pendingKey = null
      lastToken = ','
    } else if (str !== undefined) {
      if (lastToken === ':') {
        // 'key': 'giá trị' (leaf).
        if (pendingKey !== null) {
          const full = [...stack.filter(Boolean), pendingKey].join('.')
          if (KEY_RE.test(full)) keys.add(full)
          pendingKey = null
        }
        lastToken = 'str'
      } else if (lastToken === '{' || lastToken === ',') {
        pendingKey = str
        lastToken = 'str'
      } else {
        lastToken = 'str'
      }
    } else if (ident !== undefined) {
      if (lastToken === '{' || lastToken === ',') pendingKey = ident
      else if (lastToken === ':') {
        if (pendingKey !== null) {
          const full = [...stack.filter(Boolean), pendingKey].join('.')
          if (KEY_RE.test(full)) keys.add(full)
          pendingKey = null
        }
      }
      lastToken = 'ident'
    }
  }
  return keys
}

// ---------------------------------------------------------------------------
// Trích mọi giá trị key gán cho thuộc tính khai báo kiểu DictKey trong một
// file (vd labelKey: 'providers.field.id' trong bảng FIELDS).
// ---------------------------------------------------------------------------
function extractDescriptorKeyValues(source) {
  const values = new Set()
  const props = new Set()
  for (const m of source.matchAll(/([A-Za-z_$][\w$]*)\??:\s*DictKey\b/g)) props.add(m[1])
  for (const prop of props) {
    const re = new RegExp(`${prop}\\s*:\\s*['"\`](${SEGMENT}(?:\\.${SEGMENT})+)['"\`]`, 'gi')
    for (const m of source.matchAll(re)) if (KEY_RE.test(m[1])) values.add(m[1])
  }
  return values
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
async function main() {
  const wantJson = process.argv.includes('--json')
  const startedAt = new Date().toISOString()
  const errors = []
  const infos = []

  const rel = (p) => path.relative(WEB_ROOT, p).split(path.sep).join('/')

  // ---- (1) Chuỗi tiếng Việt hard-code -------------------------------------
  const scanFiles = [...SCAN_FILES]
  for (const d of SCAN_DIRS) scanFiles.push(...(await collectFiles([d])))
  const hardcoded = []
  const suppressed = []
  const consoleStrings = []
  for (const file of [...new Set(scanFiles)].sort()) {
    const source = await readFile(file, 'utf8')
    const lines = source.split('\n')
    const { strings, blanked } = splitStringsAndComments(source)
    const isSuppressed = (ln) => /i18n-ignore/.test(lines[ln - 1] ?? '')
    const isConsole = (ln) => /^\s*console\.(log|warn|error|info|debug)\b/.test(lines[ln - 1] ?? '')
    for (const s of strings) {
      if (!VI_RE.test(s.text)) continue
      const text = s.text.replace(/\s+/g, ' ').trim()
      if (ALLOWLIST.has(text)) {
        suppressed.push({ file: rel(file), line: s.line, kind: 'allowlist', text })
        continue
      }
      if (isSuppressed(s.line)) {
        suppressed.push({ file: rel(file), line: s.line, kind: 'i18n-ignore', text })
        continue
      }
      if (isConsole(s.line)) {
        consoleStrings.push({ file: rel(file), line: s.line, text })
        continue
      }
      hardcoded.push({ file: rel(file), line: s.line, kind: 'string', text })
    }
    for (const f of scanJsxText(blanked, rel(file))) {
      if (isSuppressed(f.line)) {
        suppressed.push({ file: f.file, line: f.line, kind: 'i18n-ignore', text: f.text })
        continue
      }
      hardcoded.push(f)
    }
  }

  // ---- (2) Key t() vs từ điển ---------------------------------------------
  const viPath = path.join(I18N_DIR, 'vi.ts')
  const enPath = path.join(I18N_DIR, 'en.ts')
  const dictMissing = []
  if (!existsSync(viPath)) dictMissing.push('web/src/i18n/vi.ts')
  if (!existsSync(enPath)) dictMissing.push('web/src/i18n/en.ts')
  let viKeys = new Set()
  let enKeys = new Set()
  if (existsSync(viPath)) viKeys = collectDefinedKeys(await readFile(viPath, 'utf8'))
  if (existsSync(enPath)) enKeys = collectDefinedKeys(await readFile(enPath, 'utf8'))

  const usageFiles = [
    ...(await collectFiles([SRC], USAGE_SCAN_EXCLUDE)),
  ]
  const used = new Map()
  const dynamic = []
  const tableLookups = []
  const tableKeyValues = new Set()
  for (const file of usageFiles) {
    const source = await readFile(file, 'utf8')
    const { blanked } = splitStringsAndComments(source)
    const { used: u, dynamic: d, tableLookups: tl } = extractTUsages(blanked, rel(file), source.split('\n'))
    for (const [k, locs] of u) {
      if (!used.has(k)) used.set(k, [])
      used.get(k).push(...locs)
    }
    dynamic.push(...d)
    tableLookups.push(...tl)
    for (const k of extractTableKeyValues(source)) tableKeyValues.add(k)
    for (const k of extractDescriptorKeyValues(source)) tableKeyValues.add(k)
  }
  const missVi = []
  const missEn = []
  for (const [key, locs] of [...used.entries()].sort()) {
    if (!viKeys.has(key)) missVi.push({ key, locs })
    if (!enKeys.has(key)) missEn.push({ key, locs })
  }
  const onlyVi = [...viKeys].filter((k) => !enKeys.has(k)).sort()
  const onlyEn = [...enKeys].filter((k) => !viKeys.has(k)).sort()
  // Giá trị trong bảng tra tĩnh cũng phải có trong cả hai từ điển.
  const tableMissVi = [...tableKeyValues].filter((k) => !viKeys.has(k)).sort()
  const tableMissEn = [...tableKeyValues].filter((k) => !enKeys.has(k)).sort()

  // ---- (3) Override chế độ sáng trong app.css ------------------------------
  const cssPath = path.join(SRC, 'app.css')
  const cssMissing = []
  const cssExempt = []
  let cssContent = ''
  if (existsSync(cssPath)) {
    cssContent = await readFile(cssPath, 'utf8')
    // Miễn trừ có đánh dấu: /* light-ok: .screen — lý do ... */
    const exempt = new Set()
    for (const m of cssContent.matchAll(/light-ok:\s*(\.[A-Za-z0-9_-]+)/g)) exempt.add(m[1])
    for (const sel of LIGHT_REQUIRED) {
      if (exempt.has(sel)) {
        cssExempt.push(sel)
        continue
      }
      // Cần có rule [data-mantine-color-scheme='light'] ... <selector> ...
      const ok = cssContent
        .split('\n')
        .some((ln) => ln.includes(LIGHT_ATTR) && ln.includes(sel))
      if (!ok) cssMissing.push(sel)
    }
  } else {
    cssMissing.push(...LIGHT_REQUIRED.map((s) => `${s} (thiếu file app.css)`)
    )
  }

  // ---- Tổng hợp lỗi ---------------------------------------------------------
  if (hardcoded.length > 0) errors.push(`${hardcoded.length} chuỗi tiếng Việt hard-code còn sót trong text UI`)
  if (dictMissing.length > 0) errors.push(`thiếu từ điển i18n: ${dictMissing.join(', ')}`)
  if (missVi.length > 0) errors.push(`${missVi.length} key t() thiếu trong vi.ts`)
  if (missEn.length > 0) errors.push(`${missEn.length} key t() thiếu trong en.ts`)
  if (onlyVi.length > 0 || onlyEn.length > 0) {
    errors.push(`lệch parity từ điển: ${onlyVi.length} key chỉ có ở vi.ts, ${onlyEn.length} key chỉ có ở en.ts`)
  }
  if (dynamic.length > 0) errors.push(`${dynamic.length} cách dùng t() với key động (cấm theo hợp đồng)`)
  if (tableMissVi.length > 0) errors.push(`${tableMissVi.length} key trong bảng tra/descriptor tĩnh thiếu trong vi.ts: ${tableMissVi.join(', ')}`)
  if (tableMissEn.length > 0) errors.push(`${tableMissEn.length} key trong bảng tra/descriptor tĩnh thiếu trong en.ts: ${tableMissEn.join(', ')}`)
  if (cssMissing.length > 0) errors.push(`${cssMissing.length} selector thiếu override chế độ sáng trong app.css: ${cssMissing.join(', ')}`)

  const summary = {
    startedAt,
    webRoot: WEB_ROOT,
    scope: {
      hardcodedScan: ['src/pages/', 'src/components/', 'src/App.tsx'],
      tUsageScan: ['src/ (trừ src/i18n/)'],
    },
    hardcodedCount: hardcoded.length,
    hardcodedByFile: countBy(hardcoded, 'file'),
    suppressedCount: suppressed.length,
    consoleStringsCount: consoleStrings.length,
    dictFilesMissing: dictMissing,
    definedKeys: { vi: viKeys.size, en: enKeys.size },
    usedKeysCount: used.size,
    missVi: missVi.map((x) => x.key),
    missEn: missEn.map((x) => x.key),
    onlyVi,
    onlyEn,
    dynamicTCount: dynamic.length,
    tableLookups,
    tableKeyValuesCount: tableKeyValues.size,
    tableMissVi,
    tableMissEn,
    lightMissing: cssMissing,
    lightExempt: cssExempt,
    errors,
    ok: errors.length === 0,
  }

  if (wantJson) {
    console.log(JSON.stringify({ ...summary, hardcoded, suppressed, consoleStrings, missVi, missEn, dynamic, infos }, null, 2))
  } else {
    printReport(summary, { hardcoded, suppressed, consoleStrings, missVi, missEn, dynamic, tableLookups, onlyVi, onlyEn, infos })
  }
  process.exit(errors.length === 0 ? 0 : 1)
}

function countBy(arr, field) {
  const m = {}
  for (const x of arr) m[x[field]] = (m[x[field]] ?? 0) + 1
  return m
}

function printReport(s, d) {
  const h = (t) => console.log(`\n== ${t} ==`)
  console.log(`check-i18n — Gói 0(b) Kite UI — chạy lúc ${s.startedAt}`)
  console.log(`web root: ${s.webRoot}`)
  console.log(`Phạm vi quét chuỗi: ${s.scope.hardcodedScan.join(' ')} (trừ src/i18n/)`)
  console.log(`Phạm vi quét t(): ${s.scope.tUsageScan.join(' ')}`)

  h(`(1) Chuỗi tiếng Việt hard-code trong text UI: ${s.hardcodedCount}`)
  if (s.hardcodedCount === 0) {
    console.log('OK — không còn chuỗi tiếng Việt hard-code trong phạm vi quét.')
  } else {
    for (const [file, n] of Object.entries(s.hardcodedByFile)) console.log(`  ${file}: ${n}`)
    console.log('  -- chi tiết (tối đa 200 dòng đầu) --')
    for (const f of d.hardcoded.slice(0, 200)) {
      console.log(`  ${f.file}:${f.line} [${f.kind}] ${truncate(f.text, 100)}`)
    }
    if (d.hardcoded.length > 200) console.log(`  ... và ${d.hardcoded.length - 200} dòng nữa (xem --json)`)
  }
  console.log(`Ngoại lệ đã áp dụng: ${s.suppressedCount} (allowlist / i18n-ignore)`)
  for (const f of d.suppressed.slice(0, 50)) {
    console.log(`  (ngoại lệ ${f.kind}) ${f.file}:${f.line} ${truncate(f.text, 80)}`)
  }
  console.log(`Chuỗi console (không phải text UI, chỉ ghi nhận): ${s.consoleStringsCount}`)
  for (const f of d.consoleStrings.slice(0, 20)) {
    console.log(`  (console) ${f.file}:${f.line} ${truncate(f.text, 80)}`)
  }

  h(`(2) Key t() so với từ điển vi.ts / en.ts`)
  if (s.dictFilesMissing.length > 0) {
    console.log(`THIẾU FILE TỪ ĐIỂN: ${s.dictFilesMissing.join(', ')} — chưa đối chiếu được key.`)
  }
  console.log(`Key đã định nghĩa: vi=${s.definedKeys.vi}, en=${s.definedKeys.en}; key đang dùng: ${s.usedKeysCount}`)
  if (d.missVi.length === 0 && d.missEn.length === 0 && s.dictFilesMissing.length === 0) {
    console.log('OK — mọi key dùng trong t() đều có trong cả hai từ điển.')
  }
  for (const x of d.missVi) console.log(`  MISS vi.ts: ${x.key} (dùng tại ${x.locs.map((l) => `${l.file}:${l.line}`).join(', ')})`)
  for (const x of d.missEn) console.log(`  MISS en.ts: ${x.key} (dùng tại ${x.locs.map((l) => `${l.file}:${l.line}`).join(', ')})`)
  if (d.onlyVi.length > 0) console.log(`  Chỉ có ở vi.ts (${d.onlyVi.length}): ${d.onlyVi.slice(0, 30).join(', ')}${d.onlyVi.length > 30 ? ' ...' : ''}`)
  if (d.onlyEn.length > 0) console.log(`  Chỉ có ở en.ts (${d.onlyEn.length}): ${d.onlyEn.slice(0, 30).join(', ')}${d.onlyEn.length > 30 ? ' ...' : ''}`)
  console.log(`Key động (cấm): ${s.dynamicTCount}`)
  for (const x of d.dynamic) console.log(`  DYNAMIC ${x.file}:${x.line} — ${x.reason} :: ${x.snippet}`)
  console.log(`Tra bảng/descriptor tĩnh (warning — không tính lỗi; mọi giá trị key trong bảng/descriptor đã kiểm có trong cả hai từ điển): ${d.tableLookups.length}`)
  for (const x of d.tableLookups) console.log(`  TABLE-LOOKUP ${x.file}:${x.line} — t(${x.variable}) từ bảng ${x.table} :: ${x.snippet}`)
  if (s.tableMissVi.length > 0) console.log(`  Giá trị bảng tra thiếu trong vi.ts: ${s.tableMissVi.join(', ')}`)
  if (s.tableMissEn.length > 0) console.log(`  Giá trị bảng tra thiếu trong en.ts: ${s.tableMissEn.join(', ')}`)

  h(`(3) Override chế độ sáng trong app.css`)
  if (s.lightMissing.length === 0) {
    console.log(`OK — đủ override light cho: ${LIGHT_REQUIRED.join(', ')}`)
  } else {
    console.log(`THIẾU override light cho: ${s.lightMissing.join(', ')}`)
    console.log(`  (đã có sẵn trong app.css: ${LIGHT_REQUIRED.filter((x) => !s.lightMissing.includes(x) && !s.lightExempt.includes(x)).join(', ') || 'không có selector nào'})`)
  }
  if (s.lightExempt.length > 0) {
    console.log(`Miễn trừ có đánh dấu light-ok (lý do ghi trong comment ở app.css): ${s.lightExempt.join(', ')}`)
  }

  h(s.ok ? 'KẾT QUẢ: ĐẠT (exit 0)' : `KẾT QUẢ: CÒN LỖI (exit 1) — ${s.errors.length} nhóm lỗi`)
  for (const e of s.errors) console.log(`  - ${e}`)
  if (!s.ok) console.log('Lưu ý: chạy lại script sau khi worker (a)/(b) báo xong từng trang; ghi rõ thời điểm chạy mỗi lần.')
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

main().catch((err) => {
  console.error('check-i18n: lỗi không chạy được script:', err)
  process.exit(2)
})
