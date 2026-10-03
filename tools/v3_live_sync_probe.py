"""手机输入→电脑浮窗的端到端同步时延探针（隔离数据目录、offscreen Qt、无头 Chromium）。

真实生产页面 web/dist + 真实 V3App/V3Bridge/HudController/DesktopShell；不调用插入、剪贴板、
热键，不碰日用数据和前台。手机端用 Playwright keyboard.insert_text 产生浏览器 input 事件，
模拟输入法连续上屏；电脑端在 Qt 主线程轮询浮窗正文，记录每个标记首次可见的时间。

平台替身说明：慢存储用延迟 IndexedDB 事务完成回调模拟；慢电脑存储用包裹 apply_phone_update
的 sleep 模拟；手机算力用 CDP CPU 降速模拟。它们不是真机测量。

用法：
  python tools/v3_live_sync_probe.py --src src --dist web/dist --out result.json
  （可用 --src/--dist 指向基线副本做前后对比）
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import statistics
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

MARK = re.compile(r"#(\d+)!")


def now_ms() -> float:
    return time.time() * 1000.0


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]
    return {"n": len(values), "p50": round(pick(.5), 1), "p95": round(pick(.95), 1),
            "max": round(ordered[-1], 1), "mean": round(statistics.fmean(values), 1)}


SLOW_IDB = """
(() => {
  window.__idbDelay = 0; window.__idbStats = {tx: 0, waitMs: 0};
  const desc = Object.getOwnPropertyDescriptor(IDBTransaction.prototype, 'oncomplete');
  Object.defineProperty(IDBTransaction.prototype, 'oncomplete', {configurable: true,
    get() { return desc.get.call(this); },
    set(fn) {
      const started = performance.now();
      desc.set.call(this, fn ? (e) => {
        const d = window.__idbDelay || 0;
        const finish = () => { window.__idbStats.tx++; window.__idbStats.waitMs += performance.now() - started; fn.call(this, e); };
        d ? setTimeout(finish, d) : finish();
      } : fn);
    }});
  // 测量输入事件处理（应用自身 input 监听）在主线程占用的时间。
  window.__inputCost = [];
  document.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'text') e.target.__t0 = performance.now();
  }, true);
  document.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'text' && e.target.__t0) window.__inputCost.push(performance.now() - e.target.__t0);
  }, false);
})();
"""


class Probe:
    def __init__(self, args):
        self.args = args
        self.server_seen: dict[int, float] = {}
        self.hud_seen: dict[int, float] = {}
        self.sent: dict[int, float] = {}
        self.server_updates = 0
        self.hud_polls = 0
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.scroll_request: str | None = None
        self.scroll_report: dict = {}
        self.grab_request: str | None = None
        self.results: dict = {"scenarios": {}}
        self.server_delay_s = 0.0
        self.apply_ms: list[float] = []

    def reset(self):
        with self.lock:
            self.server_seen.clear(); self.hud_seen.clear(); self.sent.clear(); self.server_updates = 0
            self.apply_ms = []


def setup_env(tmp: Path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["DT_V3_DATA_DIR"] = str(tmp / "data")
    os.environ["DT_V3_PIPE"] = f"DTLiveSyncProbe-{uuid.uuid4().hex}"


def make_png(path: Path, w: int, h: int, seed: int) -> None:
    from PIL import Image, ImageDraw
    import random
    rnd = random.Random(seed)
    image = Image.new("RGB", (w, h), (240, 240, 245))
    draw = ImageDraw.Draw(image)
    for _ in range(900):
        x, y = rnd.randrange(w), rnd.randrange(h)
        draw.rectangle([x, y, x + rnd.randrange(8, 160), y + rnd.randrange(8, 120)],
                       fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    image.save(path, "PNG")


async def drive(probe: Probe, app, base: str, code: str, tmp: Path):
    from playwright.async_api import async_playwright
    args = probe.args
    out = probe.results
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 420, "height": 860})
        await context.add_init_script(SLOW_IDB)
        page = await context.new_page()
        await page.goto(base)
        await page.locator("#sheetCard summary").first.click()
        await page.fill("#pairCode", code)
        await page.click("#pairGo")
        await page.wait_for_function("() => document.getElementById('connText').textContent.indexOf('已连接') >= 0", timeout=15000)
        await page.wait_for_function("() => document.getElementById('transferStatus').dataset.transport === 'synced'", timeout=15000)
        cdp = await context.new_cdp_session(page)
        counter = {"k": 0}

        shots = Path(args.shots) if args.shots else None
        if shots:
            shots.mkdir(parents=True, exist_ok=True)

        async def shoot(name: str):
            if not shots:
                return
            await page.screenshot(path=str(shots / f"phone-{name}.png"))
            probe.grab_request = str(shots / f"hud-{name}.png")
            for _ in range(50):
                if probe.grab_request is None:
                    break
                await asyncio.sleep(0.02)

        async def type_burst(n: int, interval_ms: int, label: str):
            await page.click("#text")
            await page.evaluate("() => {const t=document.getElementById('text');t.selectionStart=t.selectionEnd=t.value.length;}")
            await page.evaluate("() => { window.__inputCost = []; window.__idbStats = {tx:0, waitMs:0}; }")
            probe.reset()
            start = time.monotonic()
            first = counter["k"] + 1
            for i in range(n):
                counter["k"] += 1
                k = counter["k"]
                chunk = f"语音第{k}段继续说明问题#{k}!"
                with probe.lock:
                    probe.sent[k] = now_ms()
                await page.keyboard.insert_text(chunk)
                if i == n // 2:
                    await shoot(label)
                target = start + (i + 1) * interval_ms / 1000.0
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            last = counter["k"]
            typed_end = now_ms()
            deadline = time.monotonic() + args.settle
            while time.monotonic() < deadline:
                with probe.lock:
                    if last in probe.hud_seen and last in probe.server_seen:
                        break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
            phone = await page.evaluate("() => ({cost: window.__inputCost, idb: window.__idbStats, transport: document.getElementById('transferStatus').dataset.transport, value: document.getElementById('text').value.length})")
            with probe.lock:
                server_lat = [probe.server_seen[k] - probe.sent[k] for k in range(first, last + 1) if k in probe.server_seen]
                hud_lat = [probe.hud_seen[k] - probe.sent[k] for k in range(first, last + 1) if k in probe.hud_seen]
                missing_server = [k for k in range(first, last + 1) if k not in probe.server_seen]
                missing_hud = [k for k in range(first, last + 1) if k not in probe.hud_seen]
                final_hud = probe.hud_seen.get(last)
                result = {
                    "chunks": n, "interval_ms": interval_ms,
                    "server_latency_ms": summarize(server_lat),
                    "hud_latency_ms": summarize(hud_lat),
                    # 每个标记都是首次出现时间；后续更新会覆盖中间状态，缺失只表示中间版本被合并。
                    "hud_intermediate_skipped": len(missing_hud) - (1 if last in missing_hud else 0),
                    "final_visible_after_last_input_ms": round(final_hud - probe.sent[last], 1) if final_hud else None,
                    "final_visible_after_typing_end_ms": round(final_hud - typed_end, 1) if final_hud else None,
                    "final_marker_on_server": last not in missing_server,
                    "final_marker_on_hud": last not in missing_hud,
                    "server_updates_applied": probe.server_updates,
                    "server_apply_ms": summarize(probe.apply_ms),
                    "phone_input_handler_ms": summarize(phone["cost"]),
                    "phone_idb_tx": phone["idb"]["tx"],
                    "phone_idb_mean_tx_ms": round(phone["idb"]["waitMs"] / max(1, phone["idb"]["tx"]), 1),
                    "phone_transport_after": phone["transport"],
                    "phone_text_chars": phone["value"],
                }
            out["scenarios"][label] = result
            print(label, json.dumps(result, ensure_ascii=False), flush=True)

        async def wait_synced(timeout=20000):
            await page.wait_for_function("() => document.getElementById('transferStatus').dataset.transport === 'synced'", timeout=timeout)

        # A：连续语音式输入（短文）
        await type_burst(args.chunks, args.interval, "A_continuous_text")
        await wait_synced()

        # B：长文 + 连续输入
        long_text = "".join(f"长文第{i}行，用于测试很长的草稿在电脑浮窗中的跟随表现。\n" for i in range(220))
        await page.evaluate("(t) => {const el=document.getElementById('text'); el.focus(); el.selectionStart=el.selectionEnd=el.value.length;}", long_text)
        await page.keyboard.insert_text(long_text)
        await wait_synced()
        await type_burst(args.chunks, args.interval, "B_long_text")
        await wait_synced()

        # C：附三张图后继续输入（真实页面上传流程）
        images = []
        for i in range(args.images):
            path = tmp / f"probe-{i}.png"
            make_png(path, 1600, 1200, i)
            images.append(path)
        for path in images:
            await page.set_input_files("#file", str(path))
            await page.wait_for_selector("#editor.show", timeout=15000)
            await page.wait_for_function("() => document.getElementById('stage')?.dataset.ready === '1'", timeout=15000)
            await page.click("#done")
            await page.wait_for_function("() => !document.getElementById('editor').classList.contains('show')", timeout=20000)
        await page.wait_for_function(f"() => document.querySelectorAll('.attach-card[data-state=ready]').length >= {args.images}", timeout=60000)
        await wait_synced(30000)
        snap_bytes = await page.evaluate("""() => new Promise(r => {const q=indexedDB.open('doubao-typeless-v3-drafts');q.onsuccess=()=>{const tx=q.result.transaction('drafts','readonly');const g=tx.objectStore('drafts').get('current');tx.oncomplete=()=>{let n=0;try{n=JSON.stringify(g.result,(k,v)=>v instanceof Blob?{blob:v.size}:v).length}catch{};r(n)}}})""")
        out["current_record_json_chars_with_images"] = snap_bytes
        await type_burst(args.chunks, args.interval, "C_text_after_images")
        await wait_synced()

        # D：手机降速（CPU 4x）+ 慢本地存储（事务完成延迟）+ 图片
        await cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
        await page.evaluate(f"() => {{ window.__idbDelay = {args.slow_idb_ms}; }}")
        await type_burst(args.chunks, args.interval, "D_slow_phone_storage_images")
        await page.evaluate("() => { window.__idbDelay = 0; }")
        await cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
        await wait_synced(30000)

        # E：电脑存储慢（迟到 ACK）
        probe.server_delay_s = args.slow_server_ms / 1000.0
        await type_burst(max(20, args.chunks // 2), args.interval, "E_slow_pc_ack")
        probe.server_delay_s = 0.0
        await wait_synced(30000)

        # F：用户在浮窗里向上翻看时继续输入：位置不被顶走，内容仍更新，提供回到最新
        probe.scroll_request = "up"
        await asyncio.sleep(0.4)
        await type_burst(15, args.interval, "F_user_scrolled_up")
        probe.scroll_request = "report"
        await asyncio.sleep(0.4)
        out["scenarios"]["F_user_scrolled_up"]["scroll"] = dict(probe.scroll_report)
        probe.scroll_request = "bottom"
        await asyncio.sleep(0.3)

        # H：电脑确认迟到约 2 秒：手机如实提示落后，恢复后补齐
        probe.server_delay_s = 2.2
        await page.click("#text")
        h_first = counter["k"] + 1
        for i in range(3):
            counter["k"] += 1
            k = counter["k"]
            with probe.lock:
                probe.sent[k] = now_ms()
            await page.keyboard.insert_text(f"电脑忙时继续说第{k}段#{k}!")
            await asyncio.sleep(0.3)
        await asyncio.sleep(1.4)
        lag_label = await page.evaluate("() => document.getElementById('transferStatus').textContent")
        await shoot("H_late_ack")
        probe.server_delay_s = 0.0
        await wait_synced(30000)
        out["scenarios"]["H_late_ack_2s"] = {"status_while_waiting": lag_label,
            "server_text_contains_all": all(f"#{k}!" in app.draft.text for k in range(h_first, counter["k"] + 1)),
            "status_after": await page.evaluate("() => document.getElementById('transferStatus').textContent")}
        print("H", out["scenarios"]["H_late_ack_2s"], flush=True)

        # G：断线时继续写，重连后补齐
        await context.set_offline(True)
        await page.wait_for_function("() => document.getElementById('connText').textContent.indexOf('已连接') < 0", timeout=10000)
        offline_start = counter["k"] + 1
        await page.click("#text")
        for i in range(10):
            counter["k"] += 1
            k = counter["k"]
            with probe.lock:
                probe.sent[k] = now_ms()
            await page.keyboard.insert_text(f"离线第{k}段#{k}!")
            await asyncio.sleep(args.interval / 1000)
        online_at = now_ms()
        await context.set_offline(False)
        last = counter["k"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with probe.lock:
                if last in probe.hud_seen:
                    break
            await asyncio.sleep(0.05)
        with probe.lock:
            seen = probe.hud_seen.get(last)
        out["scenarios"]["G_offline_then_reconnect"] = {
            "offline_chunks": last - offline_start + 1,
            "hud_has_final_after_online_ms": round(seen - online_at, 1) if seen else None,
            "server_text_contains_all": all(f"#{k}!" in app.draft.text for k in range(offline_start, last + 1)),
        }
        print("G", out["scenarios"]["G_offline_then_reconnect"], flush=True)
        await wait_synced(30000)
        phone_text = await page.evaluate("() => document.getElementById('text').value")
        out["final_consistency"] = {
            "phone_equals_server": phone_text == app.draft.text,
            "phone_chars": len(phone_text), "server_chars": len(app.draft.text),
            "all_markers_on_server": all(f"#{k}!" in app.draft.text for k in range(1, counter["k"] + 1)),
        }
        await browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="src")
    ap.add_argument("--dist", default="web/dist")
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunks", type=int, default=60)
    ap.add_argument("--interval", type=int, default=150)
    ap.add_argument("--images", type=int, default=3)
    ap.add_argument("--slow-idb-ms", type=int, default=150)
    ap.add_argument("--slow-server-ms", type=int, default=250)
    ap.add_argument("--settle", type=float, default=15.0)
    ap.add_argument("--no-shell", action="store_true", help="不创建 DesktopShell（只测浮窗）")
    ap.add_argument("--shots", default="", help="保存手机页面与浮窗截图（无头/离屏渲染）的目录")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="dt-live-sync-", dir=os.environ.get("DT_PROBE_TMP") or None))
    setup_env(tmp)
    sys.path.insert(0, str(Path(args.src).resolve()))
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    qt = QApplication.instance() or QApplication([])
    try:  # 与正式入口一致的字体与样式，离屏截图才不会是方块字。
        from doubao_typeless.ui.desktop import STYLESHEET, apply_ui_font
        qt.setStyleSheet(STYLESHEET)
        apply_ui_font(qt)
    except Exception:
        pass
    import doubao_typeless.services.bridge_v3 as bridge_mod
    dist = Path(args.dist).resolve()
    bridge_mod._web_dist = lambda: dist
    from doubao_typeless.app import V3App
    app = V3App(data_dir=tmp / "data", port=0)
    app.hud.start()
    shell = None
    if not args.no_shell:
        from doubao_typeless.ui.desktop import DesktopShell
        shell = DesktopShell(app)
    probe = Probe(args)
    original = app.bridge._on_phone_draft

    def wrapped(data, *a, **kw):
        if probe.server_delay_s:
            time.sleep(probe.server_delay_s)
        started = time.perf_counter()
        ack = original(data, *a, **kw)
        t = now_ms()
        probe.apply_ms.append((time.perf_counter() - started) * 1000)
        text = app.draft.text
        with probe.lock:
            probe.server_updates += 1
            for m in MARK.finditer(text[-400:]):
                probe.server_seen.setdefault(int(m.group(1)), t)
        return ack
    app.bridge._on_phone_draft = wrapped
    loop = app.start_background()
    app._loop = loop
    code = app.auth.current_pairing_challenge() if hasattr(app.auth, "current_pairing_challenge") else None
    code = code or app.auth.new_pairing_challenge()
    base = f"http://127.0.0.1:{app.bridge.port or app.port}/"

    def poll():
        body = app.hud._body
        if body is None:
            return
        t = now_ms()
        text = body.toPlainText()
        with probe.lock:
            for m in MARK.finditer(text[-400:]):
                probe.hud_seen.setdefault(int(m.group(1)), t)
        if probe.grab_request:
            app.hud._widget.grab().save(probe.grab_request)
            probe.grab_request = None
        req = probe.scroll_request
        if req:
            bar = body.verticalScrollBar()
            if req == "up":
                bar.setValue(max(0, bar.maximum() // 3))
                probe.scroll_report = {"set_to": bar.value(), "max_before": bar.maximum()}
            elif req == "report":
                probe.scroll_report.update(value_after=bar.value(), max_after=bar.maximum(),
                                           latest_button_visible=bool(app.hud._latest.isVisible()),
                                           body_has_latest=bool(MARK.search(text[-60:])))
            elif req == "bottom":
                app.hud._latest.click()
            probe.scroll_request = None
        if probe.done.is_set():
            qt.quit()

    timer = QTimer()
    timer.setInterval(4)
    timer.timeout.connect(poll)
    timer.start()
    error: list[str] = []

    def runner():
        try:
            asyncio.run(drive(probe, app, base, code, tmp))
        except Exception:
            error.append(traceback.format_exc())
        finally:
            probe.done.set()
    threading.Thread(target=runner, daemon=True).start()
    qt.exec()
    probe.results["error"] = error[0] if error else None
    probe.results["config"] = {k: v for k, v in vars(args).items()}
    Path(args.out).write_text(json.dumps(probe.results, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        asyncio.run_coroutine_threadsafe(app.stop(), loop).result(15)
    except Exception:
        pass
    if error:
        print(error[0], file=sys.stderr)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
