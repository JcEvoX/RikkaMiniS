---
name: ai-reverse-workflow
description: AI 辅助安卓逆向的标准工作流（供安全研究/自有应用分析，尤其适合零基础）。当用户丢来一个 APK 说"帮我分析/看看/改一下"但没说清怎么做时，用这套流程系统地推进：先侦察→再定位→后动手→最后验证，并说明每步该调哪个 MCP 工具/技能。
---

# AI 辅助逆向工作流（零基础也能跟）

当用户给一个模糊任务（"分析这个 App""帮我改改""看看怎么回事"），别乱试，按这套标准流程推进。**每一步先想调哪个工具/技能**。

## 本机三个逆向 MCP 后端（先认环境）
逆向能力由 3 个本机 MCP 后端提供，都监听 `127.0.0.1`，**默认关闭**，需在「设置 → MCP 集成 → 逆向工作台」里一键探测/启用：
- **MTApkMcp（MT 管理器 · APK 层）**：开包、列目录、改 smali、改 AndroidManifest(AXML)、重签名、打包。工具前缀 `mt_apk_`（如 `mt_apk_open` / `mt_apk_list` / `mt_apk_edit_open` / `mt_apk_build`）。
- **SOMCP（玄星逆核 · 聚合逆向，端口 8000）**：SO/native（`so_open` / `analyze_*` / `read_disasm` / `edit_asm` / `build_so` / Unidbg 模拟）、反编译（`jadx_decompile` / `baksmali_decode` / `apk_decode`）、脱壳（`dex_unpack`）、回编签名（`smali_assemble` / `apk_rebuild` / `apk_sign`）、动态（`frida_control`）、Flutter（`flutter_blutter`）。
- **ProxyPinMcp（抓包）**：HTTP/HTTPS 抓包与请求分析，看 App 实际发了什么。

调用方式：先 `minis-mcp-cli tools <后端>` 看工具清单，再 `minis-mcp-cli call <后端> <工具> [参数]`。后端离线时，先提示用户在对应 App 里启动服务（MT：侧边栏开启 "APK MCP"；SOMCP：首页点大启动按钮；ProxyPin：开启抓包服务）。

## 铁律：别把各层搞混（最常见的错误）
- 任何 `.so` / native / ELF 任务 → 一律走 **SOMCP** 的 `so_open` + `analyze_*` + `edit_*` + `build_so`。
- **绝对不要**用 `mt_apk_open` / `mt_apk_list` 去打开或分析 `.so` 文件。
- `mt_apk_*` 只用于 APK 外层：开包、列 `lib/` 目录、smali/AXML 编辑、签名打包。
- 真实网络请求、接口签名 → 用 **ProxyPin** 抓包，别拿静态工具去猜。

## 阶段 0 · 明确目标（先问清或先推断）
- 用户到底要什么？分析 / 去广告 / 去校验 / 抓包 / 改数值 / 学习原理？
- 目标是自有 App 还是授权样本？（默认按授权/学习处理）
- 目标不清时，先做"侦察"给出发现，再让用户确认方向。

## 阶段 1 · 侦察（搞清这是个什么 App）
1. **加固识别**：看 lib/ 下的 so、assets、入口 Application 类名 → 用 `packer-identification` 技能判断有没有壳。
   - 有壳 → 先脱壳（`android-unpacking` / `rev-dex-dumper`），否则 jadx 看到的是壳代码。
2. **技术栈识别**：
   - `libil2cpp.so` + global-metadata → Unity（用 `unity-il2cpp-reverse`）
   - `libapp.so` + `libflutter.so` → Flutter（用 `flutter-reverse`）
   - `assets/www` / `app-service.js` / `index.android.bundle` → H5/uni-app/RN（用 `hybrid-h5-reverse`）
   - 否则就是原生 Java/Kotlin → jadx 直接看
3. **用 MT 管理器 MCP**（`mt_apk_open` + `mt_apk_list`）开包、看结构、看权限、看 lib/。

## 阶段 2 · 定位（找到要改/要分析的那段）
- 用 **jadx** 反编译看 Java，搜关键词定位：
  - 会员/VIP：`isVip`/`isMember`/`checkPro`（自有应用调试用）
  - 广告：`loadAd`/`showAd`/`Splash`（用 `ad-removal`）
  - 校验：`verify`/`check`/`sign`/`getPackageInfo`（自有应用/授权测试）
  - 网络：`OkHttp`/`Retrofit`/接口 URL
- native 逻辑（.so）→ 用 **SOMCP**（`so_open` + `analyze_functions` + `analyze_crypto`）或 IDA（`ida-decompile`/`rev-idapython`）。
- 静态看不明白 → **动态**：Frida hook 打印参数/返回/堆栈（`frida-scripts`/`rev-frida`），或 `rev-unicorn-debug` 模拟跑一段。
- 要看网络请求 → **ProxyPin 抓包** + `protocol-crypto-analysis` 分析签名/加密。

## 阶段 3 · 动手（改 / 提取 / 生成）
- 改 Java 层逻辑 → 改 smali（`smali-repack`）+ MT 管理器回编。
- 改 native → SOMCP patch 汇编/字节 + `build_so`。
- 动态改（不改文件）→ Frida hook / 免 root（`noroot-hook`）。
- 生成 Xposed 模块 → `xposed-module-builder`。
- 手法不确定 → 查 `reverse-patch-techniques`（去校验/改返回值/绕过总表）。

## 阶段 4 · 验证 & 复盘
- 改完回编、重签（`signature-bypass` 过签名校验）、装真机走一遍。
- 崩了看 logcat / 安全模式崩溃报告，定位是签名校验没过还是改错了。
- 完成后按下文「任务复盘模板」输出结构化总结。

## 推荐命令链（照着跑）
- 只分析一个 SO：`so_open` → `analyze_elf`(stats) → `analyze_functions` → `analyze_crypto` → `analysis_report`。
- 反编译看代码：`jadx_decompile`（dex→java）搜关键词定位类/方法；要看字节码用 `baksmali_decode`。
- 脱壳（加固 App）：先手动打开目标 App 让壳解密 dex 进内存 → `dex_unpack`（`action=pslist` 找包名 → `action=dump` 脱壳）→ 脱出的 dex 喂给 `jadx_decompile`。
- 改 APK 里的某个 SO（完整链路）：`mt_apk_open` → `mt_apk_list view=lib/<abi>` → `so_open`（用上一步的路径）→ `analyze_functions` → `edit_asm`（先 `dryRun=true` 预演）→ `build_so` → `mt_apk_edit_open` → `mt_apk_build`。
- 改 smali 并回编：`baksmali_decode` → 改 → `smali_assemble`（→dex）→ `apk_rebuild`（回编）→ `apk_sign`（签名）→ 装。
- 合并拆分包（xapk/apks）：`apk_rebuild`(action=merge) → `apk_sign`。
- 分析接口签名/加密协议：先用 **ProxyPin** 抓到请求看 sign 等参数 → 用 `protocol-crypto-analysis` 判断算法 → 若算法在 native 层用 **SOMCP** `so_*` 定位，或用 `frida_control` 起 Frida 运行时 hook 拿明文与密钥。
- 安全改 SO：改动前先 `session_history`(snapshot)，`edit_*` 先 `dryRun=true`，确认后再落，失败可回滚。等长覆盖或明确边界内的 patch 最稳妥，别指望自动搬移后续代码。
- 省磁盘：纯分析用 `mt_apk_open(temporary=true)`，任务做完 `mt_apk_close(workspaceId)` 释放，只有要反复重开或编辑打包才长期保留。

## 通用原则
- **一次一步，每步交代用了什么工具、看到了什么、下一步干嘛**，别跳步。
- 优先"改动小、可回滚"的方案（Frida 动态 > smali patch > so patch）。
- 遇到具体场景先 `use_skill` 调对口技能，按标准套路做，别凭记忆瞎试。
- 卡住就换track：静态卡了上动态、Java 层没有就去 native、单点难就抓包看全局。

## 边界
用于自有 App / 已授权样本的分析、学习、研究。不协助盗版牟利、破解他人付费服务、侵权。

## 任务复盘模板（每个完整任务收尾都追加一份）
任务（如"分析某 so""去除某校验并重打包"）完成后，在最后追加一份结构化复盘，方便存档、复现、分享：

```
## 📋 任务复盘
- **目标**：<用户这次要做什么>
- **对象**：<APK 包名 / SO 文件名 / 关键函数>
- **调用的工具**：<按顺序列出用过的 MCP 工具，如 mt_apk_open → so_open → analyze_functions → edit_asm → build_so>
- **关键改动**：<改了哪个函数/哪段字节，改成什么>
- **结果**：<成功/部分成功/失败，产物在哪>
- **验证**：<是否验证过、怎么验证的>
- **复现步骤**：<精简到下次能照着重跑的几步>
- **风险与后续**：<遗留问题、需人工确认的点>
```

若任务未完成或中途失败，也要给出简短小结说明卡在哪一步、下一步建议怎么做。
