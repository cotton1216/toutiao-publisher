#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
今日头条（头条号）自动存草稿工具
=================================

把 Markdown 文章通过浏览器自动化填进头条号后台编辑器，只到「存草稿」为止，
不点发布按钮 —— 发布这一步留给人。

用法:
    python publish.py article.md
    python publish.py article.md --cover cover.jpg --title "自定义标题"
    python publish.py article.md --dry-run        只解析，不启动浏览器
    python publish.py --login                     仅完成登录（首次请先跑这个）
    python publish.py --inspect                   打开后台并 dump 页面结构，用于校准选择器
    python publish.py article.md --keep-open      存完草稿不关浏览器，方便人工核对

前置:
    python -m pip install playwright markdown
    python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# 数据目录与代码分离：skill 重装/升级不会丢掉登录态和历史。
# 可用环境变量 TOUTIAO_DATA_DIR 覆盖。
DATA_DIR = Path(
    os.environ.get("TOUTIAO_DATA_DIR") or (Path.home() / ".workbuddy" / "toutiao-publisher")
).expanduser()

BROWSER_DATA = DATA_DIR / "browser_data"
LOGS_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
PROFILE_PATH = DATA_DIR / "profile.json"
HISTORY_PATH = DATA_DIR / "history.json"

PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
HOME_URL = "https://mp.toutiao.com/profile_v4/index"

# 标题长度硬限制（头条标题 2-30 字）
TITLE_MIN, TITLE_MAX = 2, 30

# 选择器候选表：按顺序尝试，命中第一个可见的即为目标。
# 头条前端会改版，改版后只需在这里补一条，不用动主流程。
SEL = {
    "title": [
        "textarea[placeholder*='标题']",
        "input[placeholder*='标题']",
        "textarea.byte-input__textarea",
        "div[contenteditable='true'][data-placeholder*='标题']",
        ".article-title textarea",
    ],
    "editor": [
        "div.ProseMirror",
        "div[contenteditable='true'][data-placeholder*='正文']",
        "div[contenteditable='true'].ProseMirror-focused",
        ".ql-editor",
        "div[contenteditable='true']",
    ],
    "cover_input": [
        "input[type='file'][accept*='image']",
        "input[type='file']",
    ],
    # 实测确认（2026-11-05）：
    # · 发布页没有「存草稿」按钮，内容靠自动保存，左下角显示「草稿将自动保存」→「草稿已保存」
    # · 发布页没有标签/话题输入框，标签无法自动填，只能手工补
}


# ---------------------------------------------------------------- 日志 / 截图

class Reporter:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.log_dir = LOGS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
        self._warned: list[str] = []

    def log(self, msg: str) -> None:
        if not self.quiet:
            print(f"[{datetime.now():%H:%M:%S}] {msg}")

    def warn(self, msg: str) -> None:
        self._warned.append(msg)
        print(f"[{datetime.now():%H:%M:%S}] ⚠️  {msg}")

    def ok(self, msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] ✅ {msg}")

    def err(self, msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] ❌ {msg}", file=sys.stderr)


# ---------------------------------------------------------------- Markdown 解析

def split_front_matter(text: str) -> tuple[dict, str]:
    """解析 --- 包裹的 front-matter。不引入 PyYAML，手写足够。"""
    m = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?", text, re.S)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().strip('"').strip("'")
        # 支持 tags: [a, b] / tags: a, b
        if v.startswith("[") and v.endswith("]"):
            v = v[1:-1]
        meta[k.strip().lower()] = v.strip()
    return meta, text[m.end():]


def pick_title(meta: dict, body: str, fallback: str) -> tuple[str, str]:
    """返回 (标题, 去掉标题行后的正文)。优先级：front-matter > 首个 H1 > 文件名。"""
    if meta.get("title"):
        return meta["title"], body
    m = re.search(r"^#[ \t]+(.+?)[ \t]*$", body, re.M)
    if m:
        title = m.group(1).strip()
        body = body[: m.start()] + body[m.end():]
        return title, body
    return fallback, body


def normalize_title(title: str, rep: Reporter, max_len: int = TITLE_MAX) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    # 去掉 markdown 残留标记
    title = re.sub(r"[*_`#>]", "", title).strip()
    if len(title) > max_len:
        rep.warn(f"标题 {len(title)} 字，超出头条 {max_len} 字上限，已截断：{title[:max_len]}…")
        title = title[:max_len]
    if len(title) < TITLE_MIN:
        rep.warn(f"标题仅 {len(title)} 字，低于头条 {TITLE_MIN} 字下限，可能被编辑器拒绝")
    return title


def md_to_html(md_text: str, base_dir: Path, rep: Reporter) -> str:
    """Markdown -> HTML，并把本地图片换成显式占位（避免静默丢图）。"""
    import markdown

    html_body = markdown.markdown(
        md_text,
        extensions=["extra", "sane_lists", "nl2br"],
        output_format="html5",
    )

    # 清掉潜在危险标签
    html_body = re.sub(r"<(script|style)\b.*?</\1>", "", html_body, flags=re.S | re.I)

    def fix_img(m: re.Match) -> str:
        tag = m.group(0)
        src_m = re.search(r"""src\s*=\s*["']([^"']+)["']""", tag)
        if not src_m:
            return ""
        src = src_m.group(1)
        if re.match(r"^(https?:)?//", src) or src.startswith("data:"):
            return tag  # 远程图 / base64 原样保留
        p = (base_dir / src).resolve()
        name = p.name
        if p.exists():
            rep.warn(f"正文图片是本地文件，编辑器无法直接引用，已插入占位：{name}")
        else:
            rep.warn(f"正文图片不存在，已插入占位：{src}")
        # 不带 <p>：外层已有段落包裹，再加会形成非法嵌套
        return f"【图片占位：{name} —— 请在此处手动上传】"

    html_body = re.sub(r"<img\b[^>]*>", fix_img, html_body, flags=re.I)
    return html_body


def inspect_markdown(md_text: str) -> dict:
    stats = {
        "代码块": len(re.findall(r"^```", md_text, re.M)) // 2,
        "表格": len(re.findall(r"(?:^\|.*\|[ \t]*\n){2,}", md_text, re.M)),
        "本地图片": len(re.findall(r"!\[[^\]]*\]\((?!https?:)", md_text)),
        "远程图片": len(re.findall(r"!\[[^\]]*\]\(https?:", md_text)),
    }
    return stats


# ---------------------------------------------------------------- 浏览器自动化

def first_visible(page, selectors: list[str], timeout: int = 8000):
    """按候选顺序找第一个可见元素，返回 Locator 或 None。"""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        time.sleep(0.4)
    return None


class ToutiaoPublisher:
    def __init__(
        self,
        rep: Reporter,
        headless: bool = False,
        keep_open: bool = False,
        channel: str | None = None,
    ):
        self.rep = rep
        self.headless = headless
        self.keep_open = keep_open
        self.channel = channel
        self.log_dir = rep.log_dir
        self.playwright = None
        self.context = None
        self.page = None

    # -- 生命周期 -------------------------------------------------

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        BROWSER_DATA.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        kernel = f"本机 {self.channel}" if self.channel else "内置 Chromium"
        self.rep.log(f"启动浏览器（{kernel}，用户数据目录：{BROWSER_DATA}）")

        self.playwright = sync_playwright().start()
        launch_kwargs = dict(
            user_data_dir=str(BROWSER_DATA),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.channel:
            launch_kwargs["channel"] = self.channel
        try:
            self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            if self.channel:
                self.rep.warn(f"用 {self.channel} 内核启动失败（{e}），回退到内置 Chromium")
                launch_kwargs.pop("channel", None)
                self.context = self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            else:
                raise
        try:
            self.context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin="https://mp.toutiao.com"
            )
        except Exception:
            pass  # 权限非必需，粘贴有降级方案

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15000)

    def close(self) -> None:
        if self.keep_open and self.page:
            self.rep.log("--keep-open：浏览器保持打开，请手动关闭窗口后再结束进程")
            try:
                input("按回车结束…")
            except EOFError:
                pass
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass

    def shot(self, name: str) -> Path:
        p = self.log_dir / f"{name}.png"
        try:
            self.page.screenshot(path=str(p))
            self.rep.log(f"截图 → {p}")
        except Exception as e:
            self.rep.log(f"截图失败（{name}）：{e}")
        return p

    # -- 登录 -----------------------------------------------------

    def ensure_login(self, max_wait: int = 300) -> bool:
        self.rep.log("检查登录态…")
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        time.sleep(2.5)

        if self._is_logged_in():
            self.rep.ok("已登录（登录态来自本地用户数据目录）")
            return True

        self.rep.warn("未登录，请在打开的浏览器窗口中用头条 App 扫码")
        self.shot("00-需要登录")
        print(f"\n  → 浏览器已打开登录页，请扫码。最多等待 {max_wait} 秒…\n")

        deadline = time.time() + max_wait
        while time.time() < deadline:
            time.sleep(3)
            if self._is_logged_in():
                self.rep.ok("登录成功，登录态已随用户数据目录持久化")
                return True
        self.rep.err("等待登录超时")
        self.shot("01-登录超时")
        return False

    def _is_logged_in(self) -> bool:
        """判断登录态。

        注意：未登录时头条不会跳转到 sso 域名，URL 仍停在
        mp.toutiao.com/profile_v4/index，所以**不能**用 URL 判断。
        必须以页面上的登录标志元素为准：先找未登录标志，再找已登录标志。
        """
        # 1) 明确的未登录标志（未登录页一定有「扫码登录」「验证码登录」）
        for sel in ("text=扫码登录", "text=验证码登录", "text=登录/注册", "text=立即登录"):
            try:
                if self.page.locator(sel).first.is_visible():
                    return False
            except Exception:
                continue

        # 2) 明确的已登录标志（创作者后台侧栏/工具栏）
        for sel in ("text=发头条", "text=创作中心", "text=草稿箱", "text=作品管理", "text=收益"):
            try:
                if self.page.locator(sel).first.is_visible():
                    return True
            except Exception:
                continue

        # 3) 都不命中，看本地存储里有没有会话凭证
        try:
            if self.page.evaluate(
                "() => Object.keys(localStorage).some(k => /sid|session|token|passport/i.test(k))"
            ):
                return True
        except Exception:
            pass
        return False

    # -- 填写 -----------------------------------------------------

    def open_editor(self) -> None:
        self.rep.log(f"打开发布页 {PUBLISH_URL}")
        self.page.goto(PUBLISH_URL, wait_until="domcontentloaded")
        time.sleep(4)
        self.shot("02-发布页")

    def fill_title(self, title: str) -> bool:
        loc = first_visible(self.page, SEL["title"], timeout=12000)
        if not loc:
            self.rep.err("找不到标题输入框，请用 --inspect 校准 SEL['title']")
            self.shot("03-标题框缺失")
            return False
        loc.click()
        loc.fill(title)
        time.sleep(0.6)
        actual = ""
        try:
            actual = (loc.input_value() or "").strip()
        except Exception:
            actual = (loc.inner_text() or "").strip()
        if title[:6] not in actual:
            self.rep.warn(f"标题回读异常，期望「{title}」，实际「{actual}」")
        self.rep.ok(f"标题已填入（{len(title)} 字）：{title}")
        return True

    def fill_body(self, html_body: str, plain_text: str) -> bool:
        editor = first_visible(self.page, SEL["editor"], timeout=12000)
        if not editor:
            self.rep.err("找不到正文编辑器，请用 --inspect 校准 SEL['editor']")
            self.shot("04-编辑器缺失")
            return False

        for strategy, fn in (
            ("剪贴板富文本粘贴", lambda: self._fill_by_clipboard(editor, html_body)),
            ("execCommand insertHTML", lambda: self._fill_by_execcommand(editor, html_body)),
            ("逐段纯文本输入", lambda: self._fill_by_typing(editor, plain_text)),
        ):
            if strategy == "逐段纯文本输入":
                self.rep.warn(
                    "已降级到纯文本输入 —— 表格、代码块缩进、标题层级都会丢失，"
                    "建议存成草稿后到后台手动修格式"
                )
            try:
                self.rep.log(f"正文填充策略：{strategy}")
                fn()
                time.sleep(1.2)
                got = self._body_length(editor)
                if got >= 30:
                    self.rep.ok(f"正文已填入（{got} 字，策略：{strategy}）")
                    return True
                self.rep.warn(f"策略「{strategy}」写入后正文仅 {got} 字，降级重试")
            except Exception as e:
                self.rep.warn(f"策略「{strategy}」异常：{e}")
            # 清空编辑器再试下一策略
            self._clear_editor(editor)

        self.rep.err("三种策略均未能写入正文")
        self.shot("05-正文填充失败")
        return False

    def _body_length(self, editor) -> int:
        try:
            txt = editor.inner_text() or ""
        except Exception:
            return 0
        return len(re.sub(r"\s", "", txt))

    def _clear_editor(self, editor) -> None:
        try:
            editor.click()
            self.page.keyboard.press("Meta+a" if sys.platform == "darwin" else "Control+a")
            self.page.keyboard.press("Backspace")
            time.sleep(0.4)
        except Exception:
            pass

    def _fill_by_clipboard(self, editor, html_body: str) -> None:
        self.page.evaluate(
            """async (html) => {
                const item = new ClipboardItem({
                    'text/html': new Blob([html], {type: 'text/html'}),
                    'text/plain': new Blob([html.replace(/<[^>]+>/g, '')], {type: 'text/plain'}),
                });
                await navigator.clipboard.write([item]);
            }""",
            html_body,
        )
        editor.click()
        time.sleep(0.3)
        self.page.keyboard.press("Meta+v" if sys.platform == "darwin" else "Control+v")
        time.sleep(1.5)

    def _fill_by_execcommand(self, editor, html_body: str) -> None:
        editor.click()
        time.sleep(0.3)
        self.page.evaluate(
            "(html) => { document.execCommand('insertHTML', false, html); }", html_body
        )
        time.sleep(1.5)

    def _fill_by_typing(self, editor, plain_text: str) -> None:
        editor.click()
        time.sleep(0.3)
        for para in [p.strip() for p in plain_text.split("\n") if p.strip()]:
            self.page.keyboard.type(para, delay=0)
            self.page.keyboard.press("Enter")
            time.sleep(0.05)

    # -- 封面 -----------------------------------------------------

    def upload_cover(self, cover_path: Path) -> bool:
        """上传封面。

        实测：封面区在「发文设置」面板里，且 input[type=file] 不是常驻控件，
        需要点封面 + 号后才被动态创建。所以顺序是 展开面板 → 点 + → 等 input。
        """
        if not cover_path.exists():
            self.rep.warn(f"封面文件不存在：{cover_path}")
            return False

        # 1) 展开发文设置面板
        try:
            panel = self.page.locator("text=发文设置").last
            if panel.count() and panel.is_visible():
                panel.click()
                time.sleep(2)
        except Exception:
            pass

        # 2) 没有现成 input 就点封面 + 号触发（实测：div.article-cover-add 是那个 + 方块）
        if self.page.locator("input[type='file']").count() == 0:
            for sel in (
                "div.article-cover-add",
                "div[class*='article-cover-add']",
                "[class*='article-cover'] [class*='add']",
                "[class*='cover'] [class*='upload']",
            ):
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        loc.click()
                        time.sleep(1.8)
                        self.shot("06a-点封面后")
                        break
                except Exception:
                    continue

        # 3) 等控件出现
        deadline = time.time() + 8
        while time.time() < deadline and self.page.locator("input[type='file']").count() == 0:
            time.sleep(0.5)

        inputs = self.page.locator("input[type='file']")
        n = inputs.count()
        if n == 0:
            self.rep.warn("未找到封面上传控件，封面需手动上传")
            self.shot("06b-封面控件缺失")
            return False

        for i in range(n):
            try:
                inputs.nth(i).set_input_files(str(cover_path))
                time.sleep(3)
                self.rep.ok(f"封面图已上传：{cover_path.name}")
                self.shot("06c-封面上传后")
                # 4) 关键一步：上传后弹窗仍开着，必须点「确定」才真正应用为封面。
                #    只上传不点确定，图片进的是素材库，封面位置还是空的。
                for sel in (
                    "button:has-text('确定')",
                    "[role='dialog'] button:has-text('确定')",
                    "button.byte-btn-primary:has-text('确定')",
                ):
                    try:
                        ok = self.page.locator(sel).last
                        if ok.count() and ok.is_visible():
                            ok.click()
                            time.sleep(2.5)
                            self.rep.ok("已点「确定」应用封面")
                            self.shot("06d-封面已应用")
                            return True
                    except Exception:
                        continue
                self.rep.warn("未能点到封面弹窗的「确定」，封面可能未生效，请手动确认")
                self.shot("06e-封面确定按钮缺失")
                return True
            except Exception:
                continue
        self.rep.warn("封面上传失败，请手动处理")
        self.shot("06f-封面上传失败")
        return False

    # -- 账号档案 / 标签 -------------------------------------------

    def fetch_account(self) -> dict:
        """尽力从当前页面抓账号信息，抓不到就返回空值，不阻塞流程。

        头条后台账号信息的 DOM 结构不稳定，这里只作补充手段；
        抓不到时用 --set-profile nickname=xxx 手动设置即可。
        """
        try:
            info = self.page.evaluate(
                """() => {
                    const txt = (sels) => {
                        for (const s of sels) {
                            const el = document.querySelector(s);
                            const t = el && (el.innerText || el.textContent || '').trim();
                            if (t && t.length <= 30 && !/\\n/.test(t)) return t;
                        }
                        return '';
                    };
                    return {
                        nickname: txt(["[class*='nickname']", "[class*='userName']",
                                       "[class*='user-name']", ".user-name"]),
                        user_id: txt(["[class*='userId']", "[class*='user-id']",
                                      "[class*='uid']"]),
                        category: txt(["[class*='category']", "[class*='domain']"]),
                    };
                }"""
            )
            return {k: (v or "").strip() for k, v in (info or {}).items()}
        except Exception:
            return {}

    def fill_tags(self, tags: list[str]) -> bool:
        """标签无法自动填写：实测确认发布页没有标签/话题输入框。

        保留此方法只为在流程里给出一致的提示，不做任何页面操作。
        """
        tags = [t for t in (tags or []) if t and t.strip()]
        if tags:
            self.rep.warn(f"标签无法自动填写（页面无标签入口），需手动添加：{'、'.join(tags)}")
        return False

    # -- 自动保存 -------------------------------------------------

    def _autosave_hint(self) -> str:
        """读取页面左下角的保存状态文字。

        头条写文章页**没有「存草稿」按钮**，内容靠自动保存，
        左下角初始显示「草稿将自动保存」，保存后文案会变。
        """
        try:
            txt = self.page.evaluate("() => document.body.innerText || ''")
        except Exception:
            return ""
        for line in txt.splitlines():
            s = line.strip()
            if s and ("保存" in s or re.search(r"共\s*\d+\s*字", s)):
                return s
        return ""

    def wait_autosave(self, timeout: int = 30) -> tuple[bool, str]:
        """等自动保存落盘。返回 (是否检测到已保存, 最后看到的提示文字)。"""
        self.rep.log("等待自动保存（头条无存草稿按钮，依赖自动保存）…")
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self._autosave_hint()
            if any(h in last for h in ("已保存", "保存成功", "已自动保存")):
                self.rep.ok(f"自动保存已完成（状态：{last}）")
                self.shot("08-已保存")
                return True, last
            time.sleep(2)
        self.shot("08-保存状态未确认")
        self.rep.warn(f"{timeout} 秒内未出现明确的保存完成提示，最后状态：「{last}」")
        return False, last

    # -- 调试 -----------------------------------------------------

    def dump_page(self) -> None:
        out = self.log_dir / "page-dump.txt"
        try:
            data = self.page.evaluate(
                """() => {
                const brief = (el) => ({
                    tag: el.tagName.toLowerCase(),
                    cls: (el.className || '').toString().slice(0, 120),
                    ph: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '',
                    ce: el.getAttribute('contenteditable'),
                    type: el.getAttribute('type') || '',
                    text: (el.innerText || '').trim().slice(0, 40),
                });
                return {
                    url: location.href,
                    contenteditables: [...document.querySelectorAll('[contenteditable]')].map(brief),
                    textareas: [...document.querySelectorAll('textarea, input[type=text]')].map(brief),
                    fileInputs: [...document.querySelectorAll('input[type=file]')].map(brief),
                    buttons: [...document.querySelectorAll('button, [role=button]')].map(brief),
                };
            }"""
            )
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.rep.ok(f"页面结构已导出 → {out}")
            print("\n  把上面这个文件的内容发给我，我据此校准选择器。\n")
        except Exception as e:
            self.rep.err(f"导出页面结构失败：{e}")


# ---------------------------------------------------------------- 入口

# ---------------------------------------------------------------- 用户档案 / 发布历史

DEFAULT_PROFILE = {
    "account": {"nickname": "", "user_id": "", "category": "", "followers": ""},
    "preferences": {"default_tags": [], "title_style": "", "cover_hint": ""},
    "updated_at": "",
}


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_profile() -> dict:
    """读取档案。缺失字段用默认值补齐，保证老版本档案也能安全读。"""
    raw = _load_json(PROFILE_PATH, None)
    profile = json.loads(json.dumps(DEFAULT_PROFILE))  # 深拷贝，避免污染默认值
    if isinstance(raw, dict):
        for section in ("account", "preferences"):
            src = raw.get(section)
            if isinstance(src, dict):
                profile[section].update(src)
        profile["updated_at"] = raw.get("updated_at") or ""
    return profile


def save_profile(profile: dict) -> None:
    profile["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save_json(PROFILE_PATH, profile)


def body_hash(text: str) -> str:
    """正文指纹：去掉全部空白后取 sha256 前 16 位。

    去空白是为了让「段落间多空一行」「行尾空格」这类无意义改动不改变指纹。
    """
    return hashlib.sha256(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()[:16]


def load_history() -> list[dict]:
    data = _load_json(HISTORY_PATH, {"records": []})
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return [r for r in data["records"] if isinstance(r, dict)]
    return []


def find_duplicate(title: str, bhash: str) -> dict | None:
    """标题完全相同，或正文指纹相同，任一命中即判重复。"""
    for rec in load_history():
        if title and rec.get("title") == title:
            return rec
        if bhash and rec.get("body_hash") == bhash:
            return rec
    return None


def append_history(title: str, bhash: str, account: str = "", status: str = "draft") -> None:
    data = _load_json(HISTORY_PATH, {"records": []})
    if not isinstance(data, dict):
        data = {"records": []}
    records = data.setdefault("records", [])
    if not isinstance(records, list):
        records = data["records"] = []
    records.append(
        {
            "title": title,
            "body_hash": bhash,
            "account": account,
            "status": status,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    _save_json(HISTORY_PATH, data)


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = _load_json(CONFIG_PATH, {})
    return cfg if isinstance(cfg, dict) else {}


def cmd_dry_run(md_path: Path, args, rep: Reporter, title_max: int = TITLE_MAX) -> int:
    raw = md_path.read_text(encoding="utf-8")
    meta, body = split_front_matter(raw)
    title, body = pick_title(meta, body, md_path.stem)
    title = normalize_title(title, rep, title_max)
    html_body = md_to_html(body, md_path.parent, rep)
    plain = re.sub(r"<[^>]+>", "", html_body)

    if args.dump_html:
        Path(args.dump_html).expanduser().write_text(html_body, encoding="utf-8")
        print(f"\nHTML 已导出 → {args.dump_html}")

    prof = load_profile()
    if meta.get("tags"):
        _tags = [t.strip() for t in re.split(r"[,，]", meta["tags"]) if t.strip()]
        _tag_note = ""
    else:
        _tags = list(prof["preferences"].get("default_tags") or [])
        _tag_note = "  ← 继承档案默认" if _tags else ""

    print("\n——— 解析结果（dry-run，未启动浏览器）———")
    print(f"标题      : {title}  ({len(title)} 字)")
    print(f"正文字数  : {len(re.sub(r'\s', '', plain))}")
    print(f"HTML 长度 : {len(html_body)}")
    print(f"标签      : {'、'.join(_tags) or '(无)'}{_tag_note}")
    print(f"封面      : {args.cover or meta.get('cover') or '(无，需手动上传)'}")
    stats = inspect_markdown(body)
    print("\n降级风险统计：")
    for k, v in stats.items():
        flag = "← 剪贴板模式下可保留" if k in ("代码块", "表格") and v else ""
        print(f"  {k:6s}: {v} {flag}")

    bhash = body_hash(plain)
    dup = find_duplicate(title, bhash)
    print("\n正文指纹  : " + bhash)
    if dup:
        print(f"重复检测  : 命中 —— {dup.get('created_at', '?')} 已发过「{dup.get('title', '')}」")
        if not getattr(args, "force", False):
            print("            实跑会被拦截，确需重发请加 --force")
    else:
        print("重复检测  : 未命中，可以发布")
    if prof["account"].get("nickname"):
        print(f"目标账号  : {prof['account']['nickname']}")

    if rep._warned:
        print(f"\n警告 {len(rep._warned)} 条：")
        for w in rep._warned:
            print(f"  · {w}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把 Markdown 文章填入头条号后台编辑器并保存草稿（不发布）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("markdown", nargs="?", help="Markdown 文件路径")
    ap.add_argument("--title", help="覆盖文章标题（优先于 front-matter / H1）")
    ap.add_argument("--cover", help="封面图路径")
    ap.add_argument("--dry-run", action="store_true", help="只解析并检查，不启动浏览器")
    ap.add_argument("--login", action="store_true", help="仅完成登录")
    ap.add_argument("--inspect", action="store_true", help="打开后台并导出页面结构，用于校准选择器")
    ap.add_argument("--headless", action="store_true", help="无头模式（剪贴板可能失效，不推荐）")
    ap.add_argument("--keep-open", action="store_true", help="结束后保持浏览器打开")
    ap.add_argument("--dump-html", help="把转换后的 HTML 另存到指定路径，用于排查格式丢失")
    ap.add_argument("--force", action="store_true", help="跳过重复检测，强制发布")
    ap.add_argument("--profile", action="store_true", help="查看已缓存的账号档案与偏好")
    ap.add_argument(
        "--set-profile",
        nargs="+",
        metavar="KEY=VALUE",
        help="更新档案，如 --set-profile nickname=xxx default_tags=AI,算力",
    )
    ap.add_argument("--history", action="store_true", help="查看发布历史（最近 20 条）")
    args = ap.parse_args()

    rep = Reporter()
    cfg = load_config()
    title_max = int(cfg.get("title_max") or TITLE_MAX)
    channel = (cfg.get("browser_channel") or "").strip() or None

    # ---- 档案 / 历史：纯本地读写，不启动浏览器 ----
    if args.profile:
        p = load_profile()
        acct, pref = p["account"], p["preferences"]
        print("\n——— 账号档案 ———")
        print(f"昵称     : {acct.get('nickname') or '(未设置)'}")
        print(f"头条号ID : {acct.get('user_id') or '(未设置)'}")
        print(f"领域     : {acct.get('category') or '(未设置)'}")
        print(f"粉丝数   : {acct.get('followers') or '(未设置)'}")
        print("\n——— 发布偏好 ———")
        print(f"默认标签 : {'、'.join(pref.get('default_tags') or []) or '(无)'}")
        print(f"标题风格 : {pref.get('title_style') or '(未设置)'}")
        print(f"封面要求 : {pref.get('cover_hint') or '(未设置)'}")
        print(f"\n更新于   : {p.get('updated_at') or '(从未)'}")
        print(f"档案文件 : {PROFILE_PATH}")
        return 0

    if args.set_profile:
        p = load_profile()
        acct, pref = p["account"], p["preferences"]
        for item in args.set_profile:
            if "=" not in item:
                rep.warn(f"忽略「{item}」——格式应为 key=value")
                continue
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in acct:
                acct[k] = v
            elif k == "default_tags":
                pref["default_tags"] = [t.strip() for t in re.split(r"[,，]", v) if t.strip()]
            elif k in pref:
                pref[k] = v
            else:
                rep.warn(f"未知字段「{k}」；可用：{list(acct) + list(pref)}")
        save_profile(p)
        rep.ok(f"档案已更新 → {PROFILE_PATH}")
        return 0

    if args.history:
        recs = load_history()
        if not recs:
            print("\n暂无发布记录。")
            return 0
        print(f"\n——— 发布历史（共 {len(recs)} 条，显示最近 20 条）———")
        for r in reversed(recs[-20:]):
            mark = "●" if r.get("status") == "draft" else "○"
            print(f"{mark} {r.get('created_at', '?')}  {r.get('title', '')}")
            if r.get("account"):
                print(f"  账号 {r['account']}  hash {r.get('body_hash', '')}")
        print(f"\n记录文件：{HISTORY_PATH}")
        return 0

    if args.inspect:
        pub = ToutiaoPublisher(rep, headless=False, keep_open=True, channel=channel)
        try:
            pub.start()
            if not pub.ensure_login():
                return 1
            pub.open_editor()
            pub.dump_page()
            return 0
        finally:
            pub.close()

    if args.login:
        pub = ToutiaoPublisher(rep, headless=False, keep_open=False, channel=channel)
        try:
            pub.start()
            if not pub.ensure_login():
                return 1
            info = pub.fetch_account() or {}
            fresh = {k: v for k, v in info.items() if v}
            prof = load_profile()
            if fresh:
                prof["account"].update(fresh)
                save_profile(prof)
            if fresh.get("nickname"):
                rep.ok(f"账号档案已缓存：{fresh['nickname']}")
                print(f"  档案文件：{PROFILE_PATH}")
            else:
                rep.warn("未能自动识别账号昵称，可运行：--set-profile nickname=你的昵称")
            return 0
        finally:
            pub.close()

    if not args.markdown:
        ap.error("缺少 markdown 文件参数（或使用 --login / --inspect）")

    md_path = Path(args.markdown).expanduser().resolve()
    if not md_path.exists():
        rep.err(f"文件不存在：{md_path}")
        return 1

    if args.dry_run:
        return cmd_dry_run(md_path, args, rep, title_max)

    # ---- 解析 ----
    raw = md_path.read_text(encoding="utf-8")
    meta, body = split_front_matter(raw)
    title, body = pick_title(meta, body, md_path.stem)
    if args.title:
        title = args.title
    title = normalize_title(title, rep, title_max)
    html_body = md_to_html(body, md_path.parent, rep)
    plain_text = re.sub(r"<[^>]+>", "", html_body)

    if args.dump_html:
        Path(args.dump_html).expanduser().write_text(html_body, encoding="utf-8")
        rep.log(f"HTML 已导出 → {args.dump_html}")

    cover_raw = args.cover or meta.get("cover") or ""
    cover = None
    if cover_raw:
        cover = Path(cover_raw).expanduser()
        if not cover.is_absolute():
            cover = (md_path.parent / cover).resolve()
        if not cover.exists():
            rep.warn(f"封面不存在，跳过：{cover}")
            cover = None

    stats = inspect_markdown(body)
    # 实测：剪贴板富文本通道能完整保留表格与代码块，仅在降级到纯文本时才会丢，
    # 所以这里不再预先告警，改在 fill_body 降级时提示。

    # ---- 用户档案 / 标签 / 防重 ----
    prof = load_profile()
    tags_raw = meta.get("tags") or ""
    if tags_raw:
        tags = [t.strip() for t in re.split(r"[,，]", tags_raw) if t.strip()]
    else:
        tags = list(prof["preferences"].get("default_tags") or [])
        if tags:
            rep.log(f"使用档案中的默认标签：{'、'.join(tags)}")

    bhash = body_hash(plain_text)
    if not args.force:
        dup = find_duplicate(title, bhash)
        if dup:
            rep.err(f"重复：{dup.get('created_at', '?')} 已存过草稿「{dup.get('title', '')}」")
            print("  确认要再发一次请加 --force；查看全部记录用 --history。")
            return 5

    # ---- 执行 ----
    pub = ToutiaoPublisher(
        rep, headless=args.headless, keep_open=args.keep_open, channel=channel
    )
    rc = 0
    try:
        pub.start()
        if not pub.ensure_login():
            return 1
        acct_name = prof["account"].get("nickname") or ""
        if acct_name:
            rep.log(f"目标账号：{acct_name}")
        pub.open_editor()
        if not pub.fill_title(title):
            return 2
        if not pub.fill_body(html_body, plain_text):
            return 3
        if tags:
            pub.fill_tags(tags)
        if cover:
            pub.upload_cover(cover)
        else:
            rep.warn("未指定封面，需手动上传")
        saved, hint = pub.wait_autosave()
        if saved:
            append_history(title, bhash, account=acct_name, status="draft")
            rep.ok(f"已记入发布历史（累计 {len(load_history())} 条）")
        else:
            append_history(title, bhash, account=acct_name, status="unconfirmed")
            rep.warn(f"已记入历史但标记为「未确认」（状态：{hint}），请到草稿箱核对")
            rc = 4
    except Exception:
        rep.err("执行中断：\n" + traceback.format_exc())
        try:
            pub.shot("99-异常")
        except Exception:
            pass
        rc = 9
    finally:
        pub.close()

    print("\n——— 结果 ———")
    print(f"日志与截图目录：{rep.log_dir}")
    if rc == 0:
        print("草稿已自动保存。核对入口：")
        print("  https://mp.toutiao.com/profile_v4/manage/draft")
    else:
        print(f"未完全成功（退出码 {rc}），请查看日志与截图。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
