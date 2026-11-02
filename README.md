# MIUI Task
一个适用于 小米社区 4.0 自动完成成长值任务的脚本

[![996.icu](https://img.shields.io/badge/link-996.icu-red.svg)](https://996.icu) ![GitHub](https://img.shields.io/github/license/0-8-4/miui-auto-tasks) 
![Python](https://img.shields.io/badge/python-3.7+-blue) ![DockerHub](https://github.com/0-8-4/miui-auto-tasks/actions/workflows/docker-image.yml/badge.svg)


## **关于项目**:

  受`東雲研究所` 的某位大佬启发  
  最初的源码由大佬授权 `0-8-4` 使用 `MIT` 开源   
  项目初期由`0-8-4` 和 `TardisLX` 进行维护，现已逐渐转为社区驱动
  我们认为小米社区无权在无任何回报的情况下强制要求内测用户完成 KPI 任务，因此诞生了这个脚本


## **重要声明**:
- 虽然理论上本脚本不会影响小米社区账户安全，但您需要自行承担使用本脚本的后果

- **我们不鼓励，不支持一切商业使用**
  - 鉴于项目的特殊性，我们可能在任何时间 **停止更新** 或 **删除项目**


### **使用说明**：
关于项目的详细使用方法请阅览 **[WiKi](https://github.com/0-8-4/miui-auto-tasks/wiki)**


### **登录方式（三种，按需选择）**：

| 方式 | 配置 | 适用场景 |
| --- | --- | --- |
| 账号密码 | `uid` + `password` | 常规方式；账号要求短信验证时，脚本会自动下发验证码并在终端提示输入 |
| 短信验证码 | 同上，无需额外配置 | 密码登录返回「需要验证」时自动接管：`identity/list` 取验证方式 → 下发短信 → 校验验证码 → 换取社区 Cookie |
| Cookie | `cookie`（`config.yaml`）或 `MIUI_COOKIE`（Secret） | 云端 / GitHub Actions 等收不到短信、无法交互的环境；也适合不想反复触发验证的本机运行 |

**本机（`run_local.py`）**：
```bash
python run_local.py --check        # 账密登录，需要时提示输入短信验证码
python run_local.py --cookie       # 粘贴浏览器里的社区 Cookie，完全跳过验证
python run_local.py --login        # 忽略本地缓存，强制重新账密登录
python run_local.py --no-cache     # 不读写 .miui_cookie
```
登录成功后 Cookie 会缓存到 `.miui_cookie`（已加入 `.gitignore`，仅本机），下次运行直接复用，不必每天重新登录。

**Cookie 怎么取**（三种方式，任选一种）：

1. **本机已有登录态，最快**：直接打印缓存里的 Cookie，整行复制走
   ```bash
   python run_local.py --show-cookie
   ```
2. **浏览器手动取**：Chrome/Edge 打开并登录 `https://web.vip.miui.com`，按 F12 → Network → 刷新页面 → 点任意一个 `api.vip.miui.com` 的请求 → 在 Request Headers 里找到 `Cookie:`，**复制冒号后面整行**（不是单条）。
3. **抓包工具**：Fiddler / Charles 抓手机 App 的 `api.vip.miui.com` 请求，复制 Cookie 请求头。

整行里必须有 `passToken`（微信小程序签到换 `wx_vip_ph` 用）和 `serviceToken`，其余键缺一两个不影响。Cookie 不要换行、不要带引号，粘到 Secret 里必须是一整行。

**云端（GitHub Actions）**：短信验证码无法在 Action 里输入，**推荐配置 `MIUI_COOKIE` Secret**：
`Settings > Secrets and variables > Actions > New repository secret`，名称 `MIUI_COOKIE`，值粘贴整行 Cookie。
本机登录成功后可直接 `python run_local.py --show-cookie` 打印缓存里的 Cookie 复制过去。
配置后 `MIUI_PASSWORD` 可以删掉，`MIUI_UID` 可留空（脚本会从 Cookie 里的 `userId` 读取）。
Cookie 过期（接口返回 401）时重新取一次并更新 Secret 即可，大约 1~3 个月一次。

**Actions 变量一览**（Secrets 只放凭据，Variables 放开关注释）：

| 类型 | 名称 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| Secret | `MIUI_COOKIE` | ✅ | — | 整行 Cookie，云端唯一稳定登录方式 |
| Secret | `MIUI_UID` | 可选 | 空 | 有 Cookie 时不参与登录，会被 Cookie 里的 `userId` 覆盖；填错也只告警 |
| Secret | `MIUI_PASSWORD` | 可选 | 空 | 有 Cookie 时脚本不会拿它兜底，可以删掉 |
| Secret | `MIUI_USER_AGENT` | 可选 | 内置 UA | 留空即可 |
| Secret | `DINGTALK_TOKEN` | 可选 | 空 | 钉钉机器人 webhook 的 `access_token`，配了才发通知 |
| Secret | `DINGTALK_SECRET` | 可选 | 空 | 机器人安全设置选「加签」时的密钥；选关键词则留空 |
| Variable | `MIUI_CHECK_IN` | 可选 | `true` | 签到默认开；不填也开，填 `false` 关闭 |
| Variable | `MIUI_LOGGING` | 可选 | `true` | 日志作为 artifact 上传，保留 7 天 |
| Variable | `MIUI_CARROT_PULL` | 可选 | `false` | 拔萝卜只发金币，成长值模式下会被忽略 |

Secrets 和 Variables 是两个标签页，别把 `MIUI_CHECK_IN` 建成 Secret——workflow 从 `vars` 读，建在 Secrets 里不生效。

专题页任务不需要变量：脚本每天先拉任务列表，有「浏览限时专题页」且未完成才执行，没有就一次请求都不发。


### **钉钉通知（可选）**：

配了机器人就推一条结果摘要，没配就静默跳过，任务照常执行。

**机器人怎么建**：钉钉群 → 右上角设置 → 智能群助手 → 添加机器人 → 自定义（通过 Webhook 接入）→ 安全设置勾 **加签**，复制两样东西：

- `Webhook` 里的 `access_token=xxxx` 那串 → 填 `DINGTALK_TOKEN`
- 加签密钥 `SECxxxx` → 填 `DINGTALK_SECRET`

安全设置如果选的是「自定义关键词」，`DINGTALK_SECRET` 留空即可，但关键词要能命中消息内容（消息标题固定含「小米社区」，建议把关键词设成 **小米社区**）。

**本机验证**（不跑任务，直接发一条测试消息）：

```bash
# Windows PowerShell
$env:DINGTALK_TOKEN="你的token"; $env:DINGTALK_SECRET="SEC你的密钥"; python run_local.py --notify-test
# macOS / Linux
DINGTALK_TOKEN=xxx DINGTALK_SECRET=SECxxx python run_local.py --notify-test
```

收到「小米社区任务结果」消息就说明配置对了。也可以把 `DINGTALK_TOKEN` / `DINGTALK_SECRET` 写进 `config.yaml` 顶层，本机不用每次设环境变量：

```yaml
dingtalk:
  token: xxxxx
  secret: SECxxxxx
```

**推送内容**：账号、成长值变化（如 `37 → 40（+3）`）、已执行的动作、需人工的任务、执行出错项、被忽略的非成长值任务数、耗时。只记结论，不带 Cookie、请求体之类的敏感信息。
通知失败不影响任务退出码：token 错、加签不对只会让这次没消息，脚本仍按原结果结束。


### **在 Actions 里运行**：

配置完 Secret 后不需要额外操作，工作流有两种触发方式：

| 方式 | 怎么触发 | 说明 |
| --- | --- | --- |
| 定时 | 无需操作 | `0 20 * * *`（UTC）= **北京时间每天 04:00**。GitHub 的 cron 不保证准点，常见延迟几分钟到半小时 |
| 手动 | Actions → `MIUI Task` → `Run workflow` → 选 `master` → 绿色按钮 | 随时可点，不受定时影响 |

一次运行大约 3~8 分钟。结束后：

- 想看待办明细 → 点进这次运行，展开 `Run MIUI Task` 步骤看实时日志
- 想看完整日志 → 页面底部 `Artifacts` 下载 `miui-logs`（保留 7 天）
- 想看结果摘要 → 群里收到的钉钉消息

两个容易忽略的点：

**签到是幂等的**，同一小时内手动点几次也不会重复签到——脚本先查签到页，已签就一个请求都不发。所以「今天已经跑过了」不是问题，重复触发只是白跑一遍。

**定时任务会被自动停用**：仓库连续 60 天没有任何提交时，GitHub 会停掉 schedule。收到 `This scheduled workflow is disabled` 邮件或发现日志断更，去 Actions 页点一下 `Enable workflow` 即可。


### **项目依赖**：
  1. 需要前往 Python 官网自行下载自己系统对应的 Python 版本，或使用自己系统对应的包管理安装，推荐至少 Python 3.6 以上

  ```
  https://www.python.org/downloads/
  ```

  2. Python 3 安装完成之后，请在 **项目目录** 执行以下命令安装所需模块
  ```bash
  pip install -r requirements.txt
  ```
  注意：你可能需要使用管理员权限运行命令行


### **项目介绍**：  
- [x] 单账号配置（多账号已移除，一个账号一个 Secret 即可）
- [x] 支持 Docker 部署
- [x] 支持 腾讯云函数 部署
- [x] 自动登录小米账号刷新社区 Cookie 实现自动化   
- [x] 选择性启用参与小米社区拔萝卜
- [x] 小米社区签到（**改走微信小程序通道，成长值+3**，无人机校验；**默认开启**，已签到则零请求）
- [x] 限时专题任务（无需开关，任务列表里有才做）
- [x] **任务驱动执行**：先读任务列表，**只处理能获得社区成长值的任务**，且只为「存在且未完成」的发请求  
  - “每日登录小米社区App” 成长值任务
  - “浏览帖子超过10秒” 成长值任务  
  - “点赞他人帖子” 成长值任务  
  - “浏览个人主页超过10秒” 成长值任务
  - “加入小米社区圈子” 成长值任务

&#x26A0; 请注意，脚本已改为**任务列表驱动**：每次运行先拉 `getCheckinPageCakeList`，把每条任务按标题语义映射成动作，只对「存在且未完成」的任务发请求，已完成的直接跳过，看不到任务列表时 fail-closed 一个动作都不发。任务列表接口不返回动作 ID，映射表见 `miuitask.py` 的 `TASK_RULES`。

在此基础上还有一层**成长值过滤**：按任务 `desc` 里的奖励说明（如「成长值+1，上限+1/日」「成长值+10，仅限首次」）判断该任务是否发放成长值，只保留成长值任务，其余不进计划、一次请求都不发。实测被忽略的是「升级小米社区App至最新版本」（无奖励）和「参与拔萝卜获得奖励」（发金币，不是成长值）。要看全部任务可加 `--all-tasks`。

实测当前账号只有 7 条任务，**不含「点赞他人帖子」「浏览个人主页」「加入小米社区圈子」**，因此这些请求默认不再发送（旧行为可用 `--legacy` 恢复）。无法自动完成的任务会在解析结果中标为「需人工」并说明原因：升级 App（需真机装包）。

#### 签到为什么改走微信小程序通道

App / 网页的签到接口 `mtop/planet/vip/user/checkinV2` 被人机校验罩住，不带 token 直接返回 `{"status":640,"message":"人机校验失败"}`，而 token 只能由验证码 WebView 回调给出，脚本拿不到。

微信小程序（appid `wx240a4a764023c444`）那套接口是另一条路，**没有**人机校验：

```
# 1) 用社区 Cookie 里的 passToken 换小程序登录态（sid=wx_vip，硬编码在小程序 appLoginInfo 里）
GET account.xiaomi.com/pass/serviceLogin?sid=wx_vip&_json=1&_locale=zh_CN
    cookie: passToken / userId
  → location 指向 api.vip.miui.com/wx/sts
GET <location>&clientSign=urlencode(base64(sha1("nonce=<nonce>&<ssecurity>")))
  → Set-Cookie: wx_vip_ph / wx_vip_serviceToken / wx_vip_slh

# 2) 签到前必须先看签到页，已签就一个请求都不发
GET api.vip.miui.com/mtop/planet/wechat/checkin/mypagedata
  → entity.data[] 里 title=="每日签到" 的卡片，buttons[0].showType==0（或按钮文案「已签到」）表示已签
  → 已签 / 拿不到卡片 / 状态不明确，三种情况都直接返回，不发 POST

# 3) 仅在签到页明确显示「今日未签到」时才签到
POST api.vip.miui.com/mtop/planet/wechat/member/addCommunityGrowUpPointByActionV2?wx_vip_ph=<ph>
     Origin: https://servicewechat.com
     body {"action":"WECHAT_CHECKIN_TASK"}
  → 成功 {"status":200,...}；已签过 {"status":-1,"message":"加分失败"}
```

2026/11/02 成长值明细实测：**小程序签到 +3**（同日的「浏览帖子 +1」也在明细里），证明这条通道确实计入社区成长等级的成长值，不是只加经验或金币。

#### 两个签到任务不是一回事

| 任务 | 动作 | 奖励 | 能否自动 |
| --- | --- | --- | --- |
| 小米社区微信小程序签到获成长值 | `wechat_checkin`（`WECHAT_CHECKIN_TASK`） | **+3** | 能，走小程序通道 |
| 每日签到 | `check_in`（`checkinV2`） | +1~+2 | 不能，人机校验 640，需手动点一次 |

之前把两条都映射成 `check_in`，结果小程序签到做完后「每日签到」仍显示未完成，还会对同一接口多发一次请求。现在分开映射：`wechat_checkin` 已签到就零请求；`check_in` 直接标「需人工」，不再白发一次必然 640 的 POST。

另外，任务列表的完成状态和成长值到账**不同步**：签到成功、成长值已经 +3，任务列表可能仍显示「未完成」。复查时脚本会注明这是同步延迟，不用重复跑。

前提是 Cookie 里要有 `passToken`（`run_local.py --show-cookie` 输出的整行就包含）。小程序通道不可用时不会硬退回 App 通道（那条必被 640 拦），签到会在结果里标为未完成并说明原因。根据社区相关规则，启用完成限时专题任务、拔萝卜及签到等仍可能存在风险，您需要自行承担使用本脚本的后果


#### **其他**：  
* 在使用本脚本时请临时关闭网络代理工具及广告拦截程序  
* 在服务器上使用前建议先使用服务器IP登录 `https://account.xiaomi.com`  
* 建议配合 Python3 及 Crontab 使用  
* **欢迎提供有关的思路，提交BUG以及更多完成社区其他任务方式，我们会认真对待~**


#### **贡献**：

如果你在使用过程中发现任何问题，可以 [提交 issue](https://github.com/0-8-4/miui-auto-tasks/issues/new) 或自行 Fork 修改后提交 Pull request

如果你要提交 Pull request，请确保你的代码风格和项目已有的代码保持一致，遵循 [PEP 8](https://www.python.org/dev/peps/pep-0008) ，变量命名清晰，有适当的注释


# **License**
```
MIT License

Copyright (c) 2021 東雲研究所

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
