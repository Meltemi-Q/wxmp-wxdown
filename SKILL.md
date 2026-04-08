---
name: wxmp-wxdown
description: |
  微信公众号管理工具（通过 wechat-article-exporter / wxdown 本地 API）。
  搜索公众号、查看文章列表、下载文章内容、管理关注列表、获取扫码二维码。
  触发条件：用户提到搜索公众号、公众号文章、最新文章、文章列表、关注公众号、
  取消关注、下载文章、公众号状态、扫码、二维码、竞品文章等关键词时触发。
  **重要：所有操作通过本地 CLI 脚本 wxdown-manage.py 完成，不要尝试访问 Web UI 或抓取网页。**
---

# wxdown 公众号管理

通过 wechat-article-exporter（wxdown）本地 API 管理微信公众号。
**所有操作必须通过 `scripts/wxdown-manage.py` 脚本执行，禁止尝试访问 Web UI 或用浏览器/web_fetch 操作。**

## 功能

### 查看系统状态（含登录状态）
```bash
python3 scripts/wxdown-manage.py status
```

### 扫码登录
```bash
python3 scripts/wxdown-manage.py login
```
如果输出包含 QR 码图片（markdown 格式 `![wxdown QR](/root/.openclaw/workspace/wxdown_qr_latest.png)`），
**必须在回复中原样包含该 markdown 图片语法，使用完整绝对路径，不要用 ~ 开头**。
登录需要用**微信公众号**（个人订阅号即可）扫码。凭证有效期约 4 天。

### 登录异常 / 一直让扫码 / 日报发不了
先运行：
```bash
python3 scripts/wxdown-manage.py status
```
只有 `status` 明确显示未登录/已过期，才继续运行 `login` 生成二维码。
如果 `status` 显示有效，就不要重复发二维码，继续排查文章抓取或日报脚本。

### 搜索公众号
```bash
python3 scripts/wxdown-manage.py search "关键词"
python3 scripts/wxdown-manage.py search "量子位" --size 5
```

### 获取文章列表
```bash
python3 scripts/wxdown-manage.py articles <fakeid>
python3 scripts/wxdown-manage.py articles <fakeid> --size 20
python3 scripts/wxdown-manage.py articles <fakeid> --keyword "AI"
```

### 下载文章内容
```bash
python3 scripts/wxdown-manage.py download "https://mp.weixin.qq.com/s/xxx" --format md
```
支持格式：md (markdown)、html、text

### 公众号详情
```bash
python3 scripts/wxdown-manage.py info <fakeid>
```

### 关注管理（本地关注列表）
```bash
python3 scripts/wxdown-manage.py follow "量子位" MjM5MjAxNDM4MA==
python3 scripts/wxdown-manage.py unfollow MjM5MjAxNDM4MA==
python3 scripts/wxdown-manage.py follows
```

### 查看所有关注号的最新文章
```bash
python3 scripts/wxdown-manage.py latest
python3 scripts/wxdown-manage.py latest --size 3
```

### 退出登录
```bash
python3 scripts/wxdown-manage.py logout
```

## 登录过期处理

搜索和文章功能需要公众号后台授权。如果授权过期：
1. 先运行 `status` 判断是否真的过期
2. 只有 `status` 显示未登录/已过期时，才触发 QR 码生成
3. 不能只看本地 `auth-key` 文件；必须以 `status` 的真实会话检测为准
4. 脚本会自动检测并触发 QR 码生成
5. 输出中会包含 `![wxdown QR](/root/.openclaw/workspace/wxdown_qr_latest.png)`
6. **在回复中原样保留该 markdown 图片，路径必须用当前实例的绝对路径 `/root/.openclaw/workspace/wxdown_qr_latest.png`，禁止用 `~`**
7. 告诉用户「请用微信公众号扫描二维码登录」（个人订阅号即可）
8. 登录后用户可以继续操作

## 使用示例

用户说「搜索公众号 量子位」→ 运行 `python3 scripts/wxdown-manage.py search "量子位"`
用户说「量子位最新文章」→ 先查 follows 找到 fakeid → 运行 `python3 scripts/wxdown-manage.py articles <fakeid>`
用户说「关注量子位」→ 先 search 找到 fakeid → 运行 `python3 scripts/wxdown-manage.py follow "量子位" <fakeid>`
用户说「我关注了哪些号」→ 运行 `python3 scripts/wxdown-manage.py follows`
用户说「最新文章」→ 运行 `python3 scripts/wxdown-manage.py latest`
用户说「状态」「登录异常」「登不上」「一直让我扫码」「日报发不了」→ 先运行 `python3 scripts/wxdown-manage.py status`
用户明确要二维码，且 `status` 已显示未登录/已过期 → 运行 `python3 scripts/wxdown-manage.py login`

## 与 WeRSS 的区别

- wxdown 走**微信公众号后台**（mp.weixin.qq.com），不依赖第三方服务
- 没有内置订阅系统，用本地 `follows.json` 管理关注列表
- 没有后台自动采集，需手动查询或通过 cron 定时拉取
- 支持**文章下载**（HTML/Markdown/Text 格式）

## 注意

- 登录需要有微信公众号（个人订阅号即可，不需要认证号）
- 凭证有效期约 4 天，过期需重新扫码
- **不能直接获取阅读量**（需要额外的 credential 抓包）
