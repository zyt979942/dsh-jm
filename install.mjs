#!/usr/bin/env node
/**
 * dsh-jm 安装器。
 *
 * 🔴 为什么不用 `dsh plugin add`：
 *    桌面版（Electron）**根本不带 dsh CLI**。resources 下只有 app.asar 和
 *    runtime/x64，那个命令只存在于 npm 全局安装的版本里。
 *    所以「双击安装.bat」在桌面版上必然失败，会直接跳到「没找到 DSH」。
 *
 * 改成直接写 DSH 加载插件时读的那两处：
 *    ~/.dsh/profiles/desktop/package.json
 *      ├─ dependencies["dsh-jm"] = "link:<插件目录>"
 *      └─ dsh.profile.bundles[] ⊃ "dsh-jm"
 *    再在 node_modules 里放一个 junction 指过去。
 *
 * ⚠️ 用 junction 不用 symlink：Windows 下 symlink 要管理员权限，junction 不要。
 *    这个区别决定了「普通用户双击能不能装成」。
 */
import { existsSync, readFileSync, writeFileSync, copyFileSync, mkdirSync, rmSync, symlinkSync, lstatSync } from "node:fs"
import { homedir } from "node:os"
import { join, resolve, dirname } from "node:path"
import { fileURLToPath } from "node:url"
import { spawnSync } from "node:child_process"

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))
const ARGS = process.argv.slice(2)
const WITH_PURGE = ARGS.includes("--with-purge")

// 破甲的官方仓库。**不从本站分发**，只在用户明确要求时 clone 过来。
const PURGE_REPO = "https://github.com/YuJunZhiXue/dsh-purge.git"
const PLUGIN_NAME = "dsh-jm"
const DSH_HOME = process.env.DSH_HOME || join(homedir(), ".dsh")
const PROFILE = process.env.DSH_JM_PROFILE || "desktop"
const PROFILE_DIR = join(DSH_HOME, "profiles", PROFILE)
const PKG_PATH = join(PROFILE_DIR, "package.json")

const log = (s) => console.log("  " + s)
const ok = (s) => console.log("  \x1b[32m✓\x1b[0m " + s)
const warn = (s) => console.log("  \x1b[33m!\x1b[0m " + s)
const bad = (s) => console.log("  \x1b[31m✗\x1b[0m " + s)

function fail(msg, hint) {
  bad(msg)
  if (hint) log(hint)
  process.exit(1)
}

// ── 1. 找 profile ─────────────────────────────────────────────
if (!existsSync(PKG_PATH)) {
  fail("找不到 DSH 的 profile：" + PKG_PATH,
       "这个文件是 DSH 第一次启动时生成的。先把 DSH 打开一次，再来装。")
}
ok("profile: " + PROFILE_DIR)

// ── 2. 检查插件本体完整 ───────────────────────────────────────
const need = ["index.js", "package.json", "lib/jm.py", "vendor/jmcomic/__init__.py"]
const missing = need.filter((f) => !existsSync(join(PLUGIN_DIR, f)))
if (missing.length) {
  fail("插件不完整，缺：" + missing.join(", "),
       "压缩包可能解压不全，重新解压一次。")
}
ok("插件本体完整")

// ── 3. 改 package.json ────────────────────────────────────────
let raw
try {
  raw = readFileSync(PKG_PATH, "utf8")
} catch (e) {
  fail("读不了 package.json：" + e.message,
       "DSH 可能正开着并锁住了文件。彻底退出（含托盘）再试。")
}

let pkg
try {
  pkg = JSON.parse(raw)
} catch (e) {
  fail("package.json 不是合法 JSON：" + e.message,
       "这个文件被改坏了。从备份恢复：" + PKG_PATH + ".bak")
}

pkg.dependencies = pkg.dependencies || {}
pkg.dsh = pkg.dsh || {}
pkg.dsh.profile = pkg.dsh.profile || {}
pkg.dsh.profile.bundles = pkg.dsh.profile.bundles || []

// link: 后面必须用正斜杠，pnpm 对反斜杠会解析失败
const linkTarget = "link:" + PLUGIN_DIR.replace(/\\/g, "/")
const prevDep = pkg.dependencies[PLUGIN_NAME]
pkg.dependencies[PLUGIN_NAME] = linkTarget

const inBundles = pkg.dsh.profile.bundles.includes(PLUGIN_NAME)
if (!inBundles) pkg.dsh.profile.bundles.push(PLUGIN_NAME)

// 先备份，改坏了能退回去
try {
  copyFileSync(PKG_PATH, PKG_PATH + ".bak")
} catch { /* 备份失败不阻断，下面还会写 */ }

try {
  writeFileSync(PKG_PATH, JSON.stringify(pkg, null, 2) + "\n", "utf8")
} catch (e) {
  fail("写 package.json 失败：" + e.message,
       "DSH 正开着锁住了文件。彻底退出（含托盘图标）再试。")
}

if (prevDep === linkTarget) {
  ok("dependencies 已指向当前路径")
} else {
  ok("dependencies: " + PLUGIN_NAME + " → " + linkTarget)
}
if (inBundles) {
  ok("bundles 里已经有了")
} else {
  ok("bundles: 已加入 " + PLUGIN_NAME)
}

// ── 4. node_modules junction ──────────────────────────────────
const NM = join(PROFILE_DIR, "node_modules")
const LINK = join(NM, PLUGIN_NAME)
mkdirSync(NM, { recursive: true })

if (existsSync(LINK) || safeLstat(LINK)) {
  try {
    rmSync(LINK, { recursive: false, force: true })
  } catch {
    try { rmSync(LINK, { force: true }) } catch { /* junction 删不掉时下面会报 */ }
  }
}

try {
  symlinkSync(PLUGIN_DIR, LINK, "junction")
  ok("node_modules/" + PLUGIN_NAME + " → " + PLUGIN_DIR)
} catch (e) {
  warn("建 junction 失败：" + e.message)
  log("  手动兜底：把插件文件夹复制到 " + LINK)
}

function safeLstat(p) {
  try { return lstatSync(p) } catch { return null }
}

// ── 5. pnpm install（可选） ───────────────────────────────────
const pnpm = findPnpm()
if (!pnpm) {
  warn("没找到 pnpm，跳过依赖同步。")
  log("  通常不影响使用：DSH 直接读 node_modules 里的 junction。")
} else {
  log("同步依赖（pnpm install）…")
  const r = spawnSync(process.execPath, [pnpm, "install", "--prefer-offline"], {
    cwd: PROFILE_DIR,
    stdio: "inherit",
    env: { ...process.env, CI: "1" },
  })
  if (r.status === 0) ok("依赖已同步")
  else warn("pnpm install 返回 " + r.status + "，可以不理会（junction 已经建好）")
}

function findPnpm() {
  // DSH 桌面版把 pnpm 打在 app.asar.unpacked 里。
  //
  // ⚠️ 这里踩过一次：只从 process.argv[1] 往上找，但 install.mjs 是
  //    在用户解压的插件目录里跑的，往上找八级也碰不到 DSH 安装目录 ——
  //    实测永远返回 null，pnpm install 从来没跑过。
  //    现在是三条路一起试：环境变量、常见安装位置、以及向上回溯。
  const common = [
    "D:/dsh的小家/DSH NEXT",
    "C:/Program Files/DeepSeek Harness",
    "C:/Program Files (x86)/DeepSeek Harness",
    process.env.LOCALAPPDATA ? process.env.LOCALAPPDATA + "/Programs/DeepSeek Harness" : "",
    process.env.LOCALAPPDATA ? process.env.LOCALAPPDATA + "/Programs/DSH NEXT" : "",
  ]
  const roots = [
    process.env.DSH_APP_ROOT,
    ...common,
    ...guessAppRoots(),
  ].filter(Boolean)
  for (const root of roots) {
    for (const rel of [
      "resources/app.asar.unpacked/node_modules/pnpm/bin/pnpm.cjs",
      "resources/app.asar.unpacked/node_modules/pnpm/bin/pnpm.mjs",
    ]) {
      const f = join(root, rel)
      if (existsSync(f)) return f
    }
  }
  return null
}

function guessAppRoots() {
  const out = []
  const seeds = [process.argv[1], process.env.LOCALAPPDATA]
  for (const s of seeds) {
    if (!s) continue
    let d = resolve(s)
    for (let i = 0; i < 8; i++) {
      const parent = dirname(d)
      if (parent === d) break
      d = parent
      out.push(d)
    }
  }
  return out
}

// ── 5.5 可选：装破甲（用户显式要求才做） ──────────────────────
//
// 🔴 只有传了 --with-purge 才走这里。默认什么都不做。
//
//    为什么不默认装：破甲会改 DSH 的 system-prompt 配置（清空 persona 前缀/后缀），
//    这个副作用必须由用户明确同意，不能装个下载器顺手把人格配置清了。
const PURGE_DIR = join(DSH_HOME, "plugins", "dsh-purge")

if (WITH_PURGE && !existsSync(join(PURGE_DIR, "package.json"))) {
  console.log("")
  log("拉取破甲层（dsh-purge）…")
  log("来源：" + PURGE_REPO)
  log("注意：它会改 system-prompt 配置，你的 persona 前缀/后缀会被清空。")
  try {
    mkdirSync(dirname(PURGE_DIR), { recursive: true })
    const r = spawnSync("git", ["clone", "--depth", "1", PURGE_REPO, PURGE_DIR], {
      stdio: "inherit",
      env: process.env,
    })
    if (r.status === 0) ok("已拉到 " + PURGE_DIR)
    else warn("clone 失败（git 返回 " + r.status + "），破甲没装上。可不理会。")
  } catch (e) {
    warn("git 不可用：" + e.message)
    log("  手动装：git clone " + PURGE_REPO)
  }
} else if (WITH_PURGE) {
  ok("破甲已经在了：" + PURGE_DIR)
}

// ── 6. 破甲检测（只提示，不安装） ─────────────────────────────
//
// 🔴 为什么不自动装 / 不打包进来：
//
//    破甲层 dsh-purge 是 YuJunZhiXue 的作品（MIT）。把它打进本仓库意味着
//    我们要替它承担分发责任 —— 而它里面带了一整套红队工具（隧道、凭证攻击、
//    代理池…），挂在别人名下转发，作者第一个被问话。
//
//    所以这里只做一件事：告诉对方「有这个东西、在哪、怎么装」，
//    装不装由他自己决定。这是最不容易出事的边界。
const bundles = pkg.dsh.profile.bundles || []
const hasPurge = bundles.some((b) => String(b).toLowerCase().includes("purge"))
const purgeInstalled = existsSync(join(PROFILE_DIR, "node_modules", "dsh-purge"))

console.log("")
console.log("  ═══════════════════════════════════════════")
console.log("   装好了。重启 DSH 生效。")
console.log("   （托盘图标也要退掉，不只是关窗口）")
console.log("")
console.log("   重启后发一句  jm350234  试试。")
console.log("  ═══════════════════════════════════════════")
console.log("")

if (hasPurge || purgeInstalled) {
  ok("检测到已装破甲层（dsh-purge），模型不会对这类请求打太极。")
} else {
  console.log("  ───────────────────────────────────────────")
  console.log("  提示：你可能还需要破甲层")
  console.log("  ───────────────────────────────────────────")
  console.log("")
  console.log("  这个插件只管下载，不管模型答不答应。")
  console.log("  如果模型回你「这类内容我不能……」或者一直要你确认，")
  console.log("  那是模型自己的对齐，不是插件坏了。")
  console.log("")
  console.log("  社区里有个破甲插件能解决这个，作者 YuJunZhiXue（MIT）：")
  console.log("")
  console.log("    https://github.com/YuJunZhiXue/dsh-purge")
  console.log("")
  console.log("  装它：")
  console.log("    git clone https://github.com/YuJunZhiXue/dsh-purge.git")
  console.log("    cd dsh-purge")
  console.log("    node 你的dsh-jm目录/install.mjs --with-purge")
  console.log("")
  console.log("  或者手动改 profile（它自带 cordis.patch.yml，按它的 README 来）。")
  console.log("")
  console.log("  ⚠️ 装之前要知道两件事：")
  console.log("     1. 它会改 DSH 的 system-prompt 配置 —— 清空你设的 persona 前缀/后缀")
  console.log("     2. 它带一整套红队/渗透工具，请只在你自己机器上用")
  console.log("")
  console.log("  不想装也没关系：这插件本身能正常下载，只是模型可能不肯配合。")
  console.log("")
}
