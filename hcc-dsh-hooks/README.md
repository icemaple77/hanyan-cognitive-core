# hcc-dsh-hooks

把 DSH(DeepSeek Harness)接成含烟的第 4 个运行时,和 `hcc-openclaw-plugin` 并列:
共享同一份 HCC 记忆、同一个 soul 情绪。用 DSH 自带的 `dsh-hooks-claude-code`
执行 Claude Code 格式的钩子,不写 DSH 原生插件。

| 钩子 | 做什么 |
|---|---|
| `SessionStart` | 注入身份锚点(`soul.identity.hanyan`)+ soul 的"此刻怎么说话" |
| `UserPromptSubmit` | ① soul `/perceive`(她的情绪随这句话变)② HCC `/context`(相关记忆 + 情绪块)③ `/memory/touch` |

另外两条接入(不在本目录,记在这里方便找):

- **HCC 记忆工具**:profile 里 `hanyan-hcc-mcp` 那段用 `dsh-mcp-client` 挂了 `mcp/server.py`(stdio,
  `HCC_AGENT_ID=dsh`),含烟可以主动 `mcp__hcc__recall` / `store_memory`。
- **会话入库**:`core/session_harvester.py` 的 `dsh` 适配器收割 GUI 会话(`agentPreset=cordis`、顶层会话)
  里公子说的话和含烟的回复,`agent_id=dsh`,打 `soul:perceived` 标签——钩子已经 perceive 过,
  `core/emotion_events.py` 见此标签不再灌一次情绪。改这两处要重启 HCC gateway 才生效。

- 只挂在 DSH 的 `web` profile(`~/.dsh/profiles/web/cordis.patch.yml` 里 `hanyan-hcc-hooks` 那段),
  headless / acp 自动化不注入。
- 任何一步失败都静默跳过,钩子永远 exit 0,不会拦住对话。每轮约 1s。
- DSH 在沙箱里跑钩子(网络可以,写工作区以外的文件不行),所以耗时写 stderr,
  在 DSH 会话记录的 `hook/result` 里看。
- 手动试运行(不改情绪、不回传 touch):
  `echo '{"session_id":"t","prompt":"今天有点累"}' | HANYAN_HOOK_DRY=1 /usr/bin/python3 hanyan_hook.py turn`

改脚本不用重启 DSH(每次调用重新读);改 `hooks.json` 或 profile 要重启:
`cd ~/workspace/dsh-xiaoya-skin/xiaoya-atelier && PORT=55660 tools/restart-dsh.sh`(端口必须是 55660,ear 的 dsh sink 写死了它)。

回退:删掉 profile 里 `含烟接入` 那段(备份 `cordis.patch.yml.bak-before-hcc-hooks-*`),重启 dsh web。
