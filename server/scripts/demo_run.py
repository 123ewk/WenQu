"""OPT-46 自动演示脚本:用真浏览器按新用户路径把项目跑一遍。

用法(密码只从环境变量读):

    APP_DEMO_PASSWORD=xxx uv run python scripts/demo_run.py
    # 换到生产:同一份脚本,只改地址
    APP_DEMO_PASSWORD=xxx WENQU_BASE_URL=http://<SERVER_IP> uv run python scripts/demo_run.py -n 5

路径就是讲解时的那条线:
  登录 → 新建空间 → 新建权限档位 → 签发 API Key → 切回演示空间看知识库 → 按预写问题自动提问

中间建出来的空间是空的,只用来演示"从零到能用"的操作流程;真能问出答案的是
make demo-seed 灌好文档的那个演示空间,所以最后一步要切回去。加 --live 再多跑
一步:当场上传一份文档、等它解析入库完成、再问一个只有这份稿子能答的问题
(那 22 篇是提前灌好的,灌一次等不起;这一份不,状态条是当场往前推的)。
每步截图写到 demo-out/(已在 .gitignore),投屏讲解用 --headed。

加 --tour 走的是另一条路(OPT-47):不建任何东西,只登录后点产品自己带的那套导览,
逐步验收"卡片在不在、锚点命中没命中、提问那步真问出 3 条没有"。导览是做在前端的,
这里脚本只当那个"人" —— 所以它跑通不代表导览只能靠脚本跑,只代表前端那套没坏。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_FILE = REPO_ROOT / "demo" / "questions.json"
OUT_DIR = REPO_ROOT / "demo-out"

# 这三处写死成固定名字:重复跑先查同名,不堆垃圾
FLOW_SPACE = "演示-流程体验"
FLOW_GROUP = "演示-全部库检索"
# --live 现场入库用的那一份:不在 demo/docs/ 里,所以不会被 make demo-seed 灌进去;
# 问题里的"青苔计划"只写在这份稿子中,能答对就说明这一趟流水线是真跑通的
LIVE_SAMPLE = REPO_ROOT / "demo" / "现场入库演示稿.md"
LIVE_QUESTION = {
    "id": "live",
    "ask": "青苔计划为什么最终没上线?",
    "expected": ["现场入库演示稿"],
}


class DemoFailure(Exception):
    """演示跑不下去时的人话原因。"""


def _doc_stem(filename: str) -> str:
    """引用给的是文件名(01-RAG整体链路.md),清单里的 expected 故意只写标题主干
    (RAG整体链路)—— 这样重排文档序号不用改清单。比对前把序号前缀去掉。
    """
    return re.sub(r"^\d+-", "", Path(filename).stem)


def _utf8_console() -> None:
    """Windows 控制台默认 GBK,页面里的文档标题、模型回答只要带一个不在 GBK 里的符号,
    print 就抛 UnicodeEncodeError 把演示打死(种子脚本本地实测踩过)。统一改走 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _base_url() -> str:
    # 默认 localhost 而不是 127.0.0.1:vite dev 默认只绑 IPv6(::1),写 127.0.0.1 连不上。
    return os.environ.get("WENQU_BASE_URL", "http://localhost:5173").rstrip("/")


def _client() -> httpx.Client:
    # trust_env=False:本机代理会把 127.0.0.1 一起截走,回一个含义不明的 502,
    # 掩盖"服务根本没起来"这个真问题(实测踩过)。
    return httpx.Client(base_url=f"{_base_url()}/api/v1", timeout=15.0, trust_env=False)


class DemoRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base = _base_url()
        self.username = os.environ.get("APP_DEMO_USERNAME", "demo")
        self.password = os.environ.get("APP_DEMO_PASSWORD", "")
        self.demo_space = ""
        self.page = None
        self.shot_index = 0
        self.js_errors: list[str] = []
        self.asked = 0
        self.cited = 0
        self.tour_fails: list[str] = []
        self.tour_ask_total = 0

    # ---------------- 开跑前必须成立的前提 ----------------

    def preflight(self) -> None:
        if not self.password:
            raise DemoFailure(
                "没读到 APP_DEMO_PASSWORD。演示密码不下发给前端,只能由讲解人或本脚本持有"
                "(它等于半公开,不要复用其它账号的密码)。"
            )
        if not QUESTIONS_FILE.exists():
            raise DemoFailure(f"缺预写问题清单:{QUESTIONS_FILE}")
        try:
            resp = _client().get("/demo/status")
        except httpx.HTTPError as exc:
            raise DemoFailure(
                f"读不到 {_base_url()}/api/v1/demo/status({exc.__class__.__name__}):"
                "后端在这个地址上跑着吗?"
            ) from exc
        if resp.status_code == 404:
            raise DemoFailure("这个版本的服务没有 /demo/status,演示模式还没上线")
        if resp.status_code >= 400:
            raise DemoFailure(f"/demo/status 返回 HTTP {resp.status_code}")
        status = resp.json()
        if not status.get("enabled"):
            raise DemoFailure("服务端 APP_DEMO_MODE 不是 1:开关没开,也就没有演示账号")
        if not status.get("ready"):
            raise DemoFailure(
                "演示数据未就绪(文档 "
                f"{status.get('document_ready')}/{status.get('document_total')} 入库完成),"
                "先在服务端跑 make demo-seed 并等入库 worker 跑完。"
            )
        self.demo_space = status.get("space_name") or "演示空间"
        print(
            f"目标 {self.base} | 账号 {self.username} | 演示空间「{self.demo_space}」"
            f" | 就绪文档 {status.get('document_ready')} 篇",
            flush=True,
        )

    # ---------------- 定位与截图小工具 ----------------

    def testid(self, name: str):
        return self.page.locator(f'[data-testid="{name}"]')

    def wait_visible(self, name: str, timeout_ms: int | None = None):
        loc = self.testid(name).first
        loc.wait_for(state="visible", timeout=timeout_ms or self.args.timeout_ms)
        return loc

    def shot(self, name: str) -> None:
        if self.args.no_shots:
            return
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        self.shot_index += 1
        path = OUT_DIR / f"{self.shot_index:02d}-{name}.png"
        self.page.screenshot(path=str(path), full_page=self.args.full_page)
        print(f"      截图 {path.name}", flush=True)

    def step(self, title: str, fn) -> None:
        start = time.time()
        try:
            fn()
        except (PlaywrightError, DemoFailure) as exc:
            print(f"[失败] {title}({time.time() - start:.1f}s):{exc}", file=sys.stderr, flush=True)
            raise
        print(f"[完成] {title}({time.time() - start:.1f}s)", flush=True)

    def space_names(self) -> list[str]:
        self.testid("space-switcher").first.click()
        items = self.page.locator(".menu-pop .pop-item .pop-name")
        items.first.wait_for(state="visible", timeout=5000)
        names = [items.nth(i).inner_text().strip() for i in range(items.count())]
        self._close_popover()
        return names

    def switch_space(self, name: str) -> None:
        self.testid("space-switcher").first.click()
        item = self.page.locator(".menu-pop .pop-item", has_text=name).first
        item.wait_for(state="visible", timeout=5000)
        item.click()
        self._close_popover()
        # 切空间会换令牌并重拉各页数据,留点时间
        self.page.wait_for_timeout(1000)

    def _close_popover(self) -> None:
        """空间弹层挂在侧栏下方,不关掉的话下一次点侧栏会点成"关掉弹层"本身 —— 点击有去无回,
        表现为后面那步等元素超时(本地实测踩过)。收它只能点"别的地方":点回切换器会同时命中
        "点外面关闭"和"再点一次打开",结果又开一层;Esc 这个组件不认。logo 是个不吃点击的方块。
        """
        pop = self.page.locator(".menu-pop")
        if pop.count() > 0 and pop.first.is_visible():
            self.page.locator(".logo-mark").first.click()
            pop.first.wait_for(state="hidden", timeout=5000)

    def goto_nav(self, testid: str, path: str) -> None:
        """点侧栏导航并确认路由真的跳了:导航被遮挡时点击会静默失效,
        只等目标元素要白等 30 秒才报错,报错信息还指不到根因。
        """
        self.wait_visible(testid).click()
        self.page.wait_for_url(f"**{path}", timeout=self.args.timeout_ms)
        self.page.wait_for_timeout(600)

    def current_space(self) -> str:
        return self.testid("space-switcher").first.inner_text().strip().splitlines()[0]

    def wait_space(self, name: str, timeout_s: int = 10) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.current_space() == name:
                return
            self.page.wait_for_timeout(400)
        raise DemoFailure(f"没能切到空间「{name}」,当前停在「{self.current_space()}」")

    # ---------------- 六个步骤 ----------------

    def open_login(self, browser) -> None:
        self.page = browser.new_page(viewport={"width": 1440, "height": 900})
        self.page.on("pageerror", lambda err: self.js_errors.append(str(err).splitlines()[0]))
        self.page.goto(f"{self.base}/login", wait_until="networkidle")
        self.wait_visible("demo-tip")
        if self.testid("demo-not-ready").count() > 0:
            raise DemoFailure("登录页显示演示数据未就绪:先跑 make demo-seed")

    def login(self) -> None:
        self.wait_visible("demo-fill").click()
        box = self.testid("login-username").first
        if box.input_value() != self.username:
            # 状态接口只公布账号名;真账号以环境变量为准
            box.fill(self.username)
        self.testid("login-password").first.fill(self.password)
        self.testid("login-submit").first.click()
        self.page.wait_for_url("**/chat", timeout=self.args.timeout_ms)
        self.wait_visible("chat-input")
        self.shot("login-chat")

    def create_space(self) -> None:
        if FLOW_SPACE not in self.space_names():
            # space_names() 里已经把这个 popover 关掉了,建空间要先把它点开
            self.testid("space-switcher").first.click()
            self.testid("space-create-open").first.click()
            name = self.testid("space-create-name").first
            name.wait_for(state="visible", timeout=5000)
            name.fill(FLOW_SPACE)
            self.shot("create-space")
            self.testid("space-create-submit").first.click()
        else:
            print(f"      空间「{FLOW_SPACE}」已存在,直接用它", flush=True)
            self.switch_space(FLOW_SPACE)
        self.wait_space(FLOW_SPACE)

    def _switch_on(self, testid: str) -> bool:
        """el-switch 的开关状态只看根节点上的 is-checked。data-testid 可能落在根 div,
        也可能被组件透传到内部 input,所以一律往上找根再判断,才不会"点开又点关"。"""
        root = self.testid(testid).first.locator(
            "xpath=ancestor-or-self::div[contains(@class,'el-switch')]"
        )
        return "is-checked" in (root.first.get_attribute("class") or "")

    def create_group(self) -> None:
        self.goto_nav("nav-permission-groups", "/permission-groups")
        if self.page.get_by_text(FLOW_GROUP).count() > 0:
            print(f"      档位「{FLOW_GROUP}」已存在,跳过创建", flush=True)
            return
        self.testid("group-create-open").first.click()
        name = self.testid("group-name").first
        name.wait_for(state="visible", timeout=5000)
        name.fill(FLOW_GROUP)
        for cap in ("chat", "documents"):
            self.testid(f"group-cap-{cap}").first.click()
        if not self._switch_on("group-all-kbs"):
            self.testid("group-all-kbs").first.click()
        self.shot("create-group")
        self.testid("group-submit").first.click()
        self.page.wait_for_timeout(1000)

    def create_key(self) -> None:
        self.goto_nav("nav-api-keys", "/api-keys")
        self.testid("key-create-open").first.click()
        name = self.testid("key-name").first
        name.wait_for(state="visible", timeout=5000)
        name.fill(f"demo-e2e-{time.strftime('%m%d%H%M')}")
        self._pick_option("key-group", FLOW_GROUP)
        self._pick_option("key-owner", f"({self.username}")
        self.shot("create-key")
        self.testid("key-submit").first.click()
        secret = self.testid("key-secret").first
        secret.wait_for(state="visible", timeout=self.args.timeout_ms)
        # 明文只截进截图给讲解人看,不写文件、不打印:关掉弹窗就再也取不到
        self.shot("key-secret")
        self.testid("key-created-close").first.click()

    def _pick_option(self, select_testid: str, contains: str) -> None:
        """Element Plus 的下拉面板挂在 body 上,只能按文字找。"""
        self.testid(select_testid).first.click()
        option = self.page.locator(".el-select-dropdown__item", has_text=contains).first
        option.wait_for(state="visible", timeout=5000)
        option.click()
        self.page.wait_for_timeout(300)

    def show_kb(self) -> None:
        if self.current_space() != self.demo_space:
            self.switch_space(self.demo_space)
        self.goto_nav("nav-kb", "/kb")
        card = self.testid("kb-card").first
        card.wait_for(state="visible", timeout=self.args.timeout_ms)
        self.shot("kb-list")
        card.click()
        self.page.wait_for_timeout(1500)
        self.shot("kb-detail")

    def live_upload(self) -> None:
        """一个真跑入口:当场传一份新文档,等它走完解析→分块→向量化。

        那 22 篇是提前灌好的(灌一次要几分钟,讲解时等不起),这一篇不,
        状态条是当场往前推的 —— 面试官看到的就是流水线本身在跑。
        """
        if not LIVE_SAMPLE.exists():
            raise DemoFailure(f"缺现场入库用的文档:{LIVE_SAMPLE}")
        name = LIVE_SAMPLE.name
        rows = self.page.locator(".el-table__row", has_text=name)
        if rows.count() == 0:
            self.page.set_input_files(
                '[data-testid="doc-upload"] input[type="file"]', str(LIVE_SAMPLE)
            )
            rows.first.wait_for(state="visible", timeout=self.args.timeout_ms)
        self.shot("live-uploaded")
        # 详情页自己每 2 秒刷一次文档状态,这里只读不催,别去打断画面
        deadline = time.time() + self.args.ingest_timeout
        status = ""
        while time.time() < deadline:
            status = rows.first.locator(".status-text").inner_text().strip()
            if status in {"完成", "失败"}:
                break
            self.page.wait_for_timeout(2000)
        self.shot("live-ingested")
        if status == "失败":
            raise DemoFailure(f"「{name}」入库失败,查 worker 日志")
        if status != "完成":
            raise DemoFailure(f"等了 {self.args.ingest_timeout}s,「{name}」还停在「{status}」")

    def goto_chat(self) -> None:
        self.goto_nav("nav-chat", "/chat")

    def ask_questions(self) -> None:
        questions = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))["questions"]
        planned = questions[: self.args.limit]
        if self.args.live:
            planned.append(LIVE_QUESTION)
        for q in planned:
            self.step(f"提问 {q['id']}", lambda item=q: self.ask_one(item))

    def ask_one(self, q: dict) -> None:
        box = self.wait_visible("chat-input")
        box.fill(q["ask"])
        before = self.testid("chat-answer").count()
        self.testid("chat-send").first.click()
        self.wait_answer(before)
        self.asked += 1
        files = self.open_citations()
        expected = set(q.get("expected") or [])
        hit = sorted(expected & {_doc_stem(f) for f in files})
        if not expected or hit:
            self.cited += 1
        want = sorted(expected) or ["不限"]
        print(f"      {q['id']} 引用 {files or '无'} | 期望 {want}", flush=True)
        self.shot(f"{q['id']}-answer")
        if expected and not hit:
            print("      这条没命中期望文档,讲解时别照着它追问", flush=True)

    def wait_answer(self, before: int) -> None:
        """这一轮结束的标志:多出一条回答,且"停止生成"按钮消失(它只在生成过程中存在)。
        不能拿发送按钮是否可点当信号 —— 发完输入框就清空,而它要求有文字才可点,会一直禁用。
        """
        deadline = time.time() + self.args.answer_timeout
        stop = self.page.locator('[data-testid="chat-stop"]')
        while time.time() < deadline:
            if self.testid("chat-answer").count() > before and stop.count() == 0:
                self.page.wait_for_timeout(400)
                return
            self.page.wait_for_timeout(500)
        raise DemoFailure(f"等了 {self.args.answer_timeout}s 没等到回答(模型不可用?入库没完成?)")

    def open_citations(self) -> list[str]:
        """展开最后一条回答的检索卡,打开引用抽屉,读文件名。"""
        card = self.page.locator(".msg-block").last
        btn = card.locator(".tool-btn", has_text="引用")
        if btn.count() == 0:
            head = card.locator(".tool-head")
            if head.count() == 0:
                return []
            head.last.click()
            self.page.wait_for_timeout(400)
            btn = card.locator(".tool-btn", has_text="引用")
            if btn.count() == 0:
                return []
        btn.last.click()
        self.page.wait_for_timeout(600)
        files = self.page.locator(".cite-drawer .cite-file")
        names = [files.nth(i).inner_text().strip() for i in range(files.count())]
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(200)
        return names

    # ---------------- 导览模式(OPT-47):脚本只当"人",点产品自带的导览 ----------------

    def tour_check(self, cond: bool, msg: str) -> None:
        if not cond:
            self.tour_fails.append(msg)
            print(f"      [不通过] {msg}", flush=True)

    def tour_progress_total(self) -> int:
        """步数以前端为准:读进度条上的 "3/7",不在脚本里再抄一份常量。"""
        raw = self.testid("tour-progress").first.inner_text().strip()
        try:
            return int(raw.split("/")[1])
        except (IndexError, ValueError) as exc:
            raise DemoFailure(f"进度条文案读不出总步数:{raw!r}") from exc

    def run_tour(self) -> None:
        self.wait_visible("tour-consent")
        self.shot("tour-consent")
        self.testid("tour-consent-start").first.click()
        self.wait_visible("tour-card")
        total = self.tour_progress_total()
        print(f"      导览共 {total} 步", flush=True)

        start = time.time()
        for i in range(1, total + 1):
            self.wait_visible("tour-card")
            card = self.testid("tour-card").first
            key = card.get_attribute("data-step") or ""
            title = self.testid("tour-title").first.inner_text().strip()
            prog = self.testid("tour-progress").first.inner_text().strip()
            self.tour_check(prog == f"{i}/{total}", f"第 {i} 步进度显示为 {prog!r}")
            self.tour_check(bool(key), f"第 {i} 步卡片上没有 data-step,前端契约断了")
            no_anchor = card.locator(".warn").count()
            self.tour_check(no_anchor == 0, f"第 {i} 步「{title}」锚点没命中(卡片上出现提示)")
            if key == "ask":
                self.wait_ask_gate()
                self.check_after_ask()
            if i < total:
                self.testid("tour-next").first.click()
                # 下一步常要等翻页 + 接口回来才渲染(提问步除外,上面已经等完了)
                if key != "ask":
                    self.page.wait_for_timeout(1200)
            else:
                self.testid("tour-next").first.click()
                self.page.wait_for_timeout(800)
                self.tour_check(
                    self.testid("tour-card").count() == 0, "最后一步点完「结束导览」卡片没消失"
                )
            self.shot(f"tour-{i:02d}-{title[:12]}")
        print(f"      导览全程 {time.time() - start:.1f}s", flush=True)

    def wait_ask_gate(self) -> None:
        """提问这一步的「下一步」会先置灰并显示 "提问中 n/N…",答完才放开。
        没置灰 = 自动提问根本没启动;等不到放开 = 有题目没答完。
        """
        nxt = self.testid("tour-next").first
        for _ in range(17):
            if nxt.is_disabled():
                m = re.search(r"/(\d+)", nxt.inner_text() or "")
                if m:
                    # 题数以前端为准,不在脚本里再抄一份常量
                    self.tour_ask_total = int(m.group(1))
                break
            self.page.wait_for_timeout(300)
        else:
            self.tour_check(False, "进提问步 5s 内按钮没置灰:自动提问没启动")
            return
        deadline = time.time() + self.args.tour_ask_timeout
        while nxt.is_disabled():
            if time.time() > deadline:
                self.tour_check(False, f"自动提问等了 {self.args.tour_ask_timeout}s 没跑完")
                break
            self.page.wait_for_timeout(1000)

    def check_after_ask(self) -> None:
        note = self.testid("tour-ask-note")
        if note.count() > 0:
            reason = note.first.inner_text().strip()
            self.tour_check(False, f"提问步被跳过,页面给出的原因:{reason!r}")
        if not self.tour_ask_total:
            self.tour_check(False, "提问步没读到应问题数(按钮文案里没有 n/N),无法核对回答条数")
            return
        answers = self.testid("chat-answer").count()
        self.tour_check(
            answers >= self.tour_ask_total,
            f"提问步跑完只有 {answers} 条助手回答,期望 {self.tour_ask_total} 条",
        )
        print(f"      提问步:{self.tour_ask_total} 问 {answers} 答", flush=True)

    # ---------------- 主流程 ----------------

    def run(self) -> int:
        self.preflight()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not self.args.headed)
            failed = False
            try:
                self.step("打开登录页", lambda: self.open_login(browser))
                self.step("登录演示账号", self.login)
                if self.args.tour:
                    self.step("跟着产品自带的导览走一遍", self.run_tour)
                else:
                    self.step("新建空间", self.create_space)
                    self.step("新建权限档位", self.create_group)
                    self.step("签发 API Key", self.create_key)
                    self.step("展示知识库", self.show_kb)
                    if self.args.live:
                        self.step("现场上传并入库一份文档", self.live_upload)
                    self.step("回到对话页", self.goto_chat)
                    self.ask_questions()
            except (DemoFailure, PlaywrightError) as exc:
                failed = True
                print(f"\n演示中断:{exc}", file=sys.stderr, flush=True)
                if self.page is not None and not self.args.no_shots:
                    try:
                        self.shot("failed")
                    except PlaywrightError:
                        pass
            browser.close()
        if self.js_errors:
            print(f"页面 JS 报错 {len(self.js_errors)} 条,首条:{self.js_errors[0]}", flush=True)
        if not self.args.no_shots:
            print(f"截图在 {OUT_DIR}", flush=True)
        if self.args.tour:
            if self.tour_fails:
                print(f"导览 {len(self.tour_fails)} 项不通过:", flush=True)
                for line in self.tour_fails:
                    print(f"  - {line}", flush=True)
            else:
                print("导览全部通过", flush=True)
            return 1 if (self.tour_fails or failed) else 0
        print(f"提问 {self.asked} 条,其中命中期望引用 {self.cited} 条", flush=True)
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按新用户路径自动演示问渠(内核先装:make demo-browser)"
    )
    parser.add_argument("-n", "--limit", type=int, default=6, help="提问条数,默认 6")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口,投屏讲解用")
    parser.add_argument("--no-shots", action="store_true", help="只跑流程,不写截图")
    parser.add_argument("--full-page", action="store_true", help="截整页而不是当前视口")
    parser.add_argument("--timeout-ms", type=int, default=30_000, help="等元素出现的超时")
    parser.add_argument("--answer-timeout", type=int, default=180, help="等单条回答生成的秒数")
    parser.add_argument(
        "--live",
        action="store_true",
        help="加一个真跑入口:当场上传一份文档,等它解析入库完再问它一句",
    )
    parser.add_argument("--ingest-timeout", type=int, default=300, help="等这份文档入库完的秒数")
    parser.add_argument(
        "--tour",
        action="store_true",
        help="只登录,然后跟着产品自带的导览走一遍并逐步验收(不建空间、不建档位、不发 Key)",
    )
    parser.add_argument("--tour-ask-timeout", type=int, default=240, help="等提问步答完的秒数")
    args = parser.parse_args()
    _utf8_console()
    try:
        return DemoRunner(args).run()
    except DemoFailure as exc:
        print(f"无法开始演示:{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
