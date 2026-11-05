# toutiao-publisher

把 Markdown 一键送进今日头条草稿箱的工具，基于 Playwright 驱动真实浏览器。
写成 **Agent Skill** 形态，也可以当独立命令行脚本用。

## 为什么是浏览器自动化

今日头条**没有**面向个人创作者开放的发布 API：

| 通道 | 谁能用 | 对个人创作者 |
|---|---|---|
| 头条号开放平台（同步发文） | 合作方白名单 | ❌ 无申请入口 |
| 内容源接入（RSS） | 仅新闻媒体、国家机构类账号 | ❌ 资质不符 |
| 浏览器自动化 | 任何人 | ✅ |

网上流传的 `POST /v2/article/publish`、OAuth2 换 token 之类的「头条 OpenAPI 教程」是伪造文档，不要尝试。
头条请求普遍带 `X-Bogus` 类签名校验，逆向维护成本极高且一改版就失效，浏览器自动化是投入产出比最高的路径。

## 特性

- **输入 Markdown**：自动解析 front-matter 的 `title` / `cover` / `tags`，缺失时退回首个 H1 与正文首图
- **富文本保真**：走剪贴板 HTML 通道粘贴，ProseMirror 能正确渲染表格、代码块（带行号）、引用、列表
- **只到草稿箱**：全程不触碰任何发布按钮，误发概率为零
- **登录态持久化**：Playwright persistent context，扫码一次长期有效
- **账号档案与防重**：缓存昵称、默认标签、发布历史；标题或正文指纹命中即拦截
- **三级降级**：剪贴板 → `execCommand` → 逐段纯文本，每级回读字数自检，避免静默写空
- **失败可定位**：每步截图存到 `logs/<时间戳>/`

## 环境准备

```bash
pip install playwright markdown pillow
playwright install chromium   # 可选，本机有 Chrome 时走 channel="chrome"
```

## 使用

```bash
PY=python3
SKILL=./scripts/publish.py

$PY $SKILL --login                    # 首次：扫码登录，登录态持久化
$PY $SKILL article.md --dry-run       # 预检：解析结果、降级风险、防重判定
$PY $SKILL article.md                 # 正式：填写并等自动保存
$PY $SKILL article.md --cover c.jpg   # 带封面
```

其他命令：

```bash
$PY $SKILL --profile                          # 查看账号档案
$PY $SKILL --set-profile nickname=你的昵称     # 修改档案
$PY $SKILL --history                          # 查看发布历史
$PY $SKILL --inspect                          # 导出页面结构，用于校准选择器
$PY $SKILL article.md --force                 # 跳过防重
```

## 代码与数据分离

代码不含任何状态，所有运行数据落在独立目录（默认 `~/.workbuddy/toutiao-publisher/`，
可用环境变量 `TOUTIAO_DATA_DIR` 覆盖）：

```
~/.workbuddy/toutiao-publisher/
├── browser_data/     Chrome 用户目录，登录 cookie 本体
├── profile.json      账号档案（昵称、默认标签、标题风格）
├── history.json      发布历史，防重依据
├── config.json       运行配置
└── logs/<时间戳>/    每步截图与 page-dump
```

这样 skill 升级、重装、甚至删掉重建，登录态和历史都不会丢。

## 已知限制

- **没有「存草稿」按钮**。头条靠自动保存，左下角状态从「草稿将自动保存」变为「草稿已保存」即为成功。脚本轮询这个状态作为终点信号。
- **标签无法自动填写**。发布页的「发文设置」面板只有封面 / 位置 / 广告 / 首发声明 / 合集 / 权限 / 微头条 / 作品声明，没有标签入口。`default_tags` 仅作提示输出。
- **标题硬限制 2–30 字**，超长会截断并告警，低于下限会提示可能被拒。
- **需要已开通的头条号账号**，脚本不负责注册。
- **注意发文频率**。浏览器自动化在平台侧可识别，短期高频容易触发人工审核，建议保持接近人工的节奏。

## 结构

```
├── SKILL.md           技能定义：触发词、工作流、选择器、踩坑记录
├── scripts/publish.py 主脚本（单文件，约 1000 行）
└── assets/sample.md   格式校验样例，覆盖表格/代码块/引用/图片占位
```

## 免责声明

仅供个人内容发布效率工具使用。使用前请确认符合今日头条的创作者协议，
因违反平台规则导致的账号处罚由使用者自行承担。
