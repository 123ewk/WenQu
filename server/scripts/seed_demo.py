"""演示数据播种(OPT-46):把"新用户第一次打开网站该看到的东西"一次性准备好。

跑完之后,demo 账号登录就能看到:演示空间 → 知识库(22 篇文档全部 completed)→ 3 条
跨会话记忆。自动演示脚本(scripts/demo_run.py)只负责点页面,不负责造数据 —— 数据在这里造,
因为它要真的走向量模型(一次几分钟),不适合放在浏览器演示里现场等。

为什么走 HTTP 而不是直接调服务层:演示的意义就是"这条链路真的通"。经 API 才能连带
验证注册/登录/RBAC/入库 worker/对象存储,和观众看到的路径完全一致。

前提(缺一个就会在对应步骤报错并给出可执行的下一步):
- server 已启动且 **服务端** APP_DEMO_MODE=1(脚本自己也要求 1,双保险防误跑);
- worker 与 parser 在跑(文档入库是异步的,没人消费就会等到超时);
- APP_DEMO_PASSWORD 已设置(至少 8 位;接口永不返回它);
- 空间/全局已配好可用的 embedding 模型(dev 回落全局 Key,prod 需先配空间模型)。

运行(仓库根 demo/ 里的素材就在这个仓库里):
    cd server && uv run python scripts/seed_demo.py            # 播种(可重复跑,幂等)
    cd server && uv run python scripts/seed_demo.py --dry-run  # 只打印会做什么
    cd server && uv run python scripts/seed_demo.py --purge    # 清掉演示空间(账号保留)

幂等口径:账号按 username 复用(不改密码)、空间/知识库按名字复用、文档按文件名跳过、
记忆按正文去重。--purge 只删配置里那个名字的演示空间,别的空间一律不碰。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

from app.core.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "demo" / "docs"

TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_POLL_SECONDS = 2.0
_DOC_TIMEOUT_SECONDS = 900  # 单份文档入库上限(向量化慢,留足)


class SeedError(RuntimeError):
    """可预期的失败:打印这一句就够,不需要 traceback。"""


def _base_url() -> str:
    return os.environ.get("WENQU_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _doc_files() -> list[Path]:
    return sorted(DOCS_DIR.glob("*.md"))


def _client() -> httpx.Client:
    """trust_env=False:本机有系统代理时,httpx 默认会把 127.0.0.1 也丢给代理,
    于是拿到一个含义不明的 502(实测),掩盖"server 根本没在跑"这个真问题。
    """
    return httpx.Client(base_url=_base_url(), timeout=TIMEOUT, trust_env=False)


def _check(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code // 100 != 2:
        raise SeedError(f"{what} 失败:HTTP {resp.status_code} {resp.text[:200]}")
    return resp


def _json_or_fail(resp: httpx.Response, what: str) -> dict:
    data = _check(resp, what).json()
    return data if isinstance(data, dict) else {"items": data}


def _get_list(client: httpx.Client, url: str, what: str, auth: dict) -> list[dict]:
    return list(_check(client.get(url, headers=auth), what).json())


def _preflight(client: httpx.Client, settings, dry_run: bool) -> None:
    """开关、密码、服务端演示模式、素材目录 —— 四件事在动手前一次查清。"""
    if not settings.demo_mode:
        raise SeedError(
            "APP_DEMO_MODE=0,拒绝播种(这是有意的一键防误跑:先 export APP_DEMO_MODE=1 再执行)"
        )
    if not dry_run and len(settings.demo_password) < 8:
        raise SeedError("APP_DEMO_PASSWORD 未设置或少于 8 位(注册接口要求至少 8 位)")
    if not DOCS_DIR.is_dir():
        raise SeedError(f"素材目录不存在:{DOCS_DIR}")

    resp = client.get("/api/v1/demo/status")
    if resp.status_code == 404:
        raise SeedError(
            f"{_base_url()} 上没有 /api/v1/demo/status —— 服务进程跑的是不含演示模式的旧版本"
            "(切到本分支代码后重启 server)"
        )
    if resp.status_code // 100 != 2:
        raise SeedError(
            f"{_base_url()}/api/v1/demo/status 返回 HTTP {resp.status_code}"
            "(server 未就绪或在重启中)"
        )
    status = resp.json()
    if not status.get("enabled"):
        raise SeedError(
            f"服务端({_base_url()})的 APP_DEMO_MODE 没开 —— 请在服务端环境里置 1 后重启 server"
        )


def _ensure_user(client: httpx.Client, settings) -> dict:
    """有则登录,无则注册。已存在时绝不改密码(密码是运维给的,不是脚本的)。"""
    login = client.post(
        "/api/v1/auth/login",
        json={"username": settings.demo_username, "password": settings.demo_password},
    )
    if login.status_code == 200:
        return login.json()

    if login.status_code != 401:
        raise SeedError(f"登录演示账号异常:HTTP {login.status_code} {login.text[:200]}")
    print(f"  演示账号 {settings.demo_username} 不存在,注册一个新账号")
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "username": settings.demo_username,
            "nickname": "演示账号",
            "password": settings.demo_password,
        },
    )
    return _json_or_fail(reg, "注册演示账号")


def _ensure_space(client: httpx.Client, auth: dict, name: str) -> str:
    existing = next(
        (s for s in _get_list(client, "/api/v1/spaces", "读取我的空间", auth)
         if s["name"] == name),
        None,
    )
    if existing is not None:
        return str(existing["id"])
    body = {"name": name, "description": "自动演示用的空间,里面只有公开的示例文档"}
    resp = client.post("/api/v1/spaces", json=body, headers=auth)
    return str(_json_or_fail(resp, "创建演示空间")["id"])


def _ensure_kb(
    client: httpx.Client, auth: dict, space_id: str, name: str
) -> str:
    base = f"/api/v1/spaces/{space_id}/knowledge-bases"
    existing = next(
        (k for k in _get_list(client, base, "读取知识库列表", auth) if k["name"] == name),
        None,
    )
    if existing is not None:
        return str(existing["id"])
    body = {"name": name, "description": "大模型应用工程问答演示知识库"}
    resp = client.post(base, json=body, headers=auth)
    return str(_json_or_fail(resp, "创建知识库")["id"])


def _existing_filenames(client: httpx.Client, auth: dict, space_id: str, kb_id: str) -> set[str]:
    base = f"/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents"
    names: set[str] = set()
    offset = 0
    while True:
        resp = _check(
            client.get(base, params={"limit": 100, "offset": offset}, headers=auth),
            "读取文档列表",
        )
        page = resp.json()
        names.update(item["filename"] for item in page["items"])
        offset += 100
        if offset >= page["total"]:
            return names


def _wait_completed(client: httpx.Client, auth: dict, space_id: str, kb_id: str,
                    doc_id: str) -> str:
    """轮询到终态;不自己认领任务(worker 可能同时在跑,抢到会误报结果)。"""
    url = f"/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}"
    deadline = time.monotonic() + _DOC_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            status = str(client.get(url, headers=auth).json()["status"])
        except (httpx.HTTPError, KeyError, ValueError):
            # 建档后秒级读详情偶发失败(见 go_live_smoke 同款观测):不打断轮询,超时再报
            status = "reading"
        if status in ("completed", "failed"):
            return status
        time.sleep(_POLL_SECONDS)
    return "timeout"


def _seed_documents(client: httpx.Client, auth: dict, space_id: str,
                    kb_id: str) -> tuple[int, int]:
    """返回 (本次新上传, 跳过)。上传后统一等待终态,别一份一份串行等。"""
    base = f"/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents"
    have = _existing_filenames(client, auth, space_id, kb_id)
    files = _doc_files()
    if not files:
        raise SeedError(f"{DOCS_DIR} 里没有 .md 素材")

    uploaded: list[tuple[str, str]] = []
    skipped = 0
    for path in files:
        if path.name in have:
            skipped += 1
            continue
        resp = client.post(
            base,
            headers=auth,
            files={"file": (path.name, path.read_bytes(), "text/markdown")},
        )
        uploaded.append((path.name, str(_json_or_fail(resp, f"上传 {path.name}")["id"])))

    print(f"  素材 {len(files)} 篇:新上传 {len(uploaded)} 篇,已存在跳过 {skipped} 篇")
    if not uploaded:
        return 0, skipped

    print("  等待入库完成(worker + parser 需在跑)...")
    failed: list[str] = []
    for name, doc_id in uploaded:
        status = _wait_completed(client, auth, space_id, kb_id, doc_id)
        mark = {"completed": "✓", "failed": "✗", "timeout": "⏱"}.get(status, "?")
        print(f"    {mark} {name[:44]:44} {status}", flush=True)
        if status != "completed":
            failed.append(f"{name}({status})")
    if failed:
        raise SeedError("以下文档未入库成功:" + ", ".join(failed))
    return len(uploaded), skipped


_SEED_MEMORIES: tuple[tuple[str, str, str], ...] = (
    ("fact", "本项目知识库", "这个知识库只收大模型应用工程方向的文档,不含公司内部资料。"),
    ("decision", "检索参数基线",
     "混合检索以 RRF k=60、向量权重 0.7 / 全文权重 0.3、相似度阈值 0.35 为基线。"),
    ("fact", "回答风格", "回答要给出引用编号,没检索到依据时直接说不知道,不要编造。"),
)


def _seed_memories(client: httpx.Client, auth: dict, space_id: str) -> int:
    """种 3 条跨会话记忆,让演示里"记忆召回"那一步有东西可召回。

    服务端按正文去重,重复跑不会产生第二条;但会消耗 embedding 调用,故先比对已有内容。
    """
    url = f"/api/v1/spaces/{space_id}/memories"
    existing = {m["content"] for m in _get_list(client, url, "读取已有记忆", auth)}
    added = 0
    for kind, topic, content in _SEED_MEMORIES:
        if content in existing:
            continue
        resp = client.post(
            url, json={"content": content, "kind": kind, "topic": topic}, headers=auth
        )
        _json_or_fail(resp, f"写入记忆 {topic}")
        added += 1
    print(f"  记忆:新增 {added} 条,已有 {len(_SEED_MEMORIES) - added} 条")
    return added


def _purge(client: httpx.Client, settings) -> None:
    login = client.post(
        "/api/v1/auth/login",
        json={"username": settings.demo_username, "password": settings.demo_password},
    )
    if login.status_code != 200:
        raise SeedError("登录不上演示账号,无法清理(先确认 APP_DEMO_PASSWORD 是否正确)")
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    targets = [
        s for s in client.get("/api/v1/spaces", headers=auth).json()
        if s["name"] == settings.demo_space_name
    ]
    if not targets:
        print(f"  没有名为「{settings.demo_space_name}」的空间,无需清理")
        return
    for space in targets:
        space_id = str(space["id"])
        base = f"/api/v1/spaces/{space_id}/knowledge-bases"
        for kb in _check(client.get(base, headers=auth), "读取知识库列表").json():
            # 先删库:服务层会顺带清对象存储前缀;直接删空间会留下孤儿对象
            _check(client.delete(f"{base}/{kb['id']}", headers=auth), f"删除知识库 {kb['name']}")
        _check(client.delete(f"/api/v1/spaces/{space_id}", headers=auth), "删除演示空间")
        print(f"  已删除空间「{space['name']}」及其知识库(账号与登录态保留)")


def main() -> int:
    args = argparse.ArgumentParser(description="播种/清理 OPT-46 演示数据")
    args.add_argument("--dry-run", action="store_true", help="只做前置检查并打印计划,不写库")
    args.add_argument("--purge", action="store_true", help="删除演示空间(演示账号保留)")
    opts = args.parse_args()

    settings = get_settings()
    print(f"目标:{_base_url()}  账号={settings.demo_username}  "
          f"空间={settings.demo_space_name}  知识库={settings.demo_kb_name}")

    with _client() as client:
        try:
            _preflight(client, settings, opts.dry_run)
            if opts.purge:
                _purge(client, settings)
                return 0
            if opts.dry_run:
                print("  [dry-run] 前置检查通过;将执行:确保账号 → 确保空间/知识库 → "
                      f"上传最多 {len(_doc_files())} 篇文档 → 写 {len(_SEED_MEMORIES)} 条记忆")
                return 0

            session = _ensure_user(client, settings)
            auth = {"Authorization": f"Bearer {session['access_token']}"}
            space_id = _ensure_space(client, auth, settings.demo_space_name)
            kb_id = _ensure_kb(client, auth, space_id, settings.demo_kb_name)
            _seed_documents(client, auth, space_id, kb_id)
            _seed_memories(client, auth, space_id)

            # 活动空间设为演示空间:登录后第一屏就落在演示场景里,省一次切换
            client.post(
                "/api/v1/auth/switch-space",
                json={"space_id": space_id, "refresh_token": session["refresh_token"]},
                headers=auth,
            )
            status = _check(client.get("/api/v1/demo/status"), "读取演示状态").json()
            print(f"完成:document_ready={status.get('document_ready')}/{status.get('document_total')}"
                  f"  ready={status.get('ready')}")
            print("下一步:make demo-browser 装一次浏览器内核,再 make demo-run 跑自动演示")
            return 0 if status.get("ready") else 1
        except SeedError as err:
            print(f"✗ {err}", file=sys.stderr)
            return 2
        except httpx.HTTPError as err:
            where = err.request.url if err.request is not None else _base_url()
            print(f"✗ 请求 {where} 失败:{err}", file=sys.stderr)
            print("  server 是否在跑?(默认 http://127.0.0.1:8000,可用 WENQU_BASE_URL 指定)",
                  file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
