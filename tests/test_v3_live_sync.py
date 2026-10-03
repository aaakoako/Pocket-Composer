"""连续输入同步链路与 PR12 review 修复的回归测试（隔离数据、无头 Chromium、offscreen Qt）。

端到端时延另见 tools/v3_live_sync_probe.py；这里只断言行为与有界性，不断言机器相关的毫秒数。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.test_v3_phone_storage import storage_page, storage_site  # noqa: F401  (pytest fixtures)

SLOW_TX = """(ms) => {
  const desc = Object.getOwnPropertyDescriptor(IDBTransaction.prototype, 'oncomplete');
  Object.defineProperty(IDBTransaction.prototype, 'oncomplete', {configurable: true,
    get() { return desc.get.call(this); },
    set(fn) { desc.set.call(this, fn ? (e) => setTimeout(() => fn.call(this, e), ms) : fn); }});
}"""


def test_flush_is_not_starved_by_continuous_saves(storage_page):
    page, _, _ = storage_page
    page.evaluate(SLOW_TX, 60)
    result = page.evaluate('''async()=>{
      const d=i=>({schema:1,text:"说话"+i,revision:i,draft_id:"d",epoch:"e",assets:[],saved_at:i});
      repo.save(d(1));
      const started=performance.now();let i=1,stop=false;
      const typing=setInterval(()=>{if(!stop)repo.save(d(++i));},15);
      await repo.flush();
      const waited=performance.now()-started;stop=true;clearInterval(typing);
      await repo.flush();
      return {waited,last:(await repo.load()).revision,i};
    }''')
    # 旧实现要等整个队列清空：持续输入时会一直等下去。现在最多等在途的一次加自己这一次。
    assert result["waited"] < 400, result
    assert result["last"] == result["i"]


def test_large_fields_are_written_once_as_payloads_and_swept(storage_page):
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      const big="data:image/png;base64,"+"Q".repeat(3*1024*1024);
      const scene=JSON.stringify({source:{url:big},ops:[1,2,3]});
      const d=t=>({schema:1,text:t,revision:t.length,draft_id:"d",epoch:"e",saved_at:1,
        assets:[{id:"a",asset_id:"x",preview:big,source:big,scene,caption:"图注",render_revision:2,status:"ready"}]});
      await repo.claim();
      for(const t of ["一","一二","一二三"]) repo.save(d(t));
      await repo.flush();
      const raw=await new Promise(r=>{const q=indexedDB.open("doubao-typeless-v3-drafts");q.onsuccess=()=>{
        const tx=q.result.transaction("drafts","readonly"),s=tx.objectStore("drafts");
        const c=s.get("current"),k=s.getAllKeys();tx.oncomplete=()=>{q.result.close();r({current:c.result,keys:k.result})}}});
      const loaded=await repo.load();
      // 图片被移除后，原图 payload 不再被引用，清理后删除；仍被引用的保留。
      repo.save({...d("一二三四"),assets:[]});await repo.flush();
      const removed=await repo.sweep();
      const after=await new Promise(r=>{const q=indexedDB.open("doubao-typeless-v3-drafts");q.onsuccess=()=>{
        const tx=q.result.transaction("drafts","readonly"),k=tx.objectStore("drafts").getAllKeys();tx.oncomplete=()=>{q.result.close();r(k.result)}}});
      return {recordChars:JSON.stringify(raw.current).length,payloads:raw.keys.filter(k=>k.startsWith("payload-")).length,
        same:loaded.assets[0].preview===big&&loaded.assets[0].source===big&&loaded.assets[0].scene===scene,
        caption:loaded.assets[0].caption,removed,left:after.filter(k=>k.startsWith("payload-")).length};
    }''')
    assert result["recordChars"] < 4000, result
    assert result["payloads"] == 2  # data URL 与场景各一份，同内容的 preview/source 共享
    assert result["same"] and result["caption"] == "图注"
    assert result["removed"] == 2 and result["left"] == 0


def test_payload_swept_by_other_tab_is_rewritten_from_memory(storage_page):
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      const big="data:image/png;base64,"+"Z".repeat(100000);
      const d=(t,assets)=>({schema:1,text:t,revision:t.length,draft_id:"d",epoch:"e",saved_at:1,assets});
      await repo.claim();
      repo.save(d("有图",[{id:"a",preview:big}]));await repo.flush();
      repo.save(d("删图",[]));await repo.flush();
      await repo.sweep();
      // 撤销删除：同一字符串再次出现，事务内发现 payload 已被清理，用内存内容补写。
      repo.save(d("撤销",[{id:"a",preview:big}]));await repo.flush();
      const fresh=await new DTStore.DraftRepository().load();
      return fresh.assets[0].preview===big;
    }''')
    assert result is True


def test_takeover_preserves_different_image_edits(storage_page):
    """PR12 review：仅白板/图片未保存编辑不同的快照也必须另存，恢复/删除副本按完整内容判断。"""
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      const a=new DTStore.DraftRepository(()=>{},()=>{},'review-a');
      const b=new DTStore.DraftRepository(()=>{},()=>{},'review-b');
      const old={schema:1,text:'same',revision:1,draft_id:'d',epoch:'e',saved_at:1,
        assets:[{id:'board',asset_id:'image',render_revision:1,caption:'',edit_scene:'old',edit_text:{value:'old',x:1,y:1}}]};
      await a.claim();a.save(old);await a.flush();await b.claim();
      const edited=structuredClone(old);edited.assets[0].edit_scene='new drawing';edited.assets[0].edit_text.value='new words';
      a.save(edited);try{await a.flush()}catch{}
      const sides=await b.sideDrafts();
      // 内容不同的副本不能被“相同”判断误删；完全相同的才删。
      await b.discardSide(a.supersededKey, old);
      const kept=(await b.sideDrafts()).length;
      await b.discardSide(a.supersededKey, edited);
      const gone=(await b.sideDrafts()).length;
      const progressOnly=structuredClone(old);progressOnly.assets[0].progress=50;
      return {sameContent:DTStore.sameContent(old,edited),progressSame:DTStore.sameContent(old,progressOnly),
        current:(await b.load()).assets[0],side:sides[0]?.draft.assets[0],sideCount:sides.length,kept,gone};
    }''')
    assert result["sameContent"] is False and result["progressSame"] is True
    assert result["sideCount"] == 1
    assert result["side"]["edit_scene"] == "new drawing" and result["side"]["edit_text"]["value"] == "new words"
    assert result["current"]["edit_scene"] == "old"
    assert result["kept"] == 1 and result["gone"] == 0


def test_outbox_pipeline_script():
    import subprocess
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run(["node", str(root / "tests" / "test_v3_live_sync_outbox.mjs")], cwd=root,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "live outbox ok" in out.stdout


# ---- 服务器：上传续传、素材解析缓存 ----

def _png(width=40, height=30, color=(10, 20, 30)) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


def test_upload_survives_restart_and_stale_chunks_are_bounded(tmp_path, monkeypatch):
    import hashlib
    from doubao_typeless.services import assets as mod
    from doubao_typeless.storage.asset_store import AssetStore
    from doubao_typeless.storage.db import V3DB
    store = AssetStore(tmp_path / "assets")
    db = V3DB(tmp_path / "db.sqlite")
    try:
        payload = _png()
        svc = mod.UploadService(store, db, chunk_size=1024)
        up = svc.init(mime="image/png", total_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
                      width=40, height=30, owner_session_id="device:phone")
        svc.put_chunk(up["upload_id"], 0, payload[:1024], "device:phone")
        uploads = store.root / "uploads"
        (uploads / up["upload_id"] / "1.tmp").write_bytes(b"half")  # 写到一半的分块不能算收到
        # 旧版本无清单的分块残留：只有过期后才删；新近的不动（可能正在初始化）。
        legacy, fresh_legacy = uploads / ("a" * 32), uploads / ("b" * 32)
        for folder in (legacy, fresh_legacy):
            folder.mkdir()
            (folder / "0.part").write_bytes(b"x" * 10)
        aged = time.time() - mod.UPLOAD_TTL_S - 60
        os.utime(legacy / "0.part", (aged, aged)); os.utime(legacy, (aged, aged))
        # uploads 下不是本服务创建的目录（名字、内容不符）或链接目标，一律保留。
        foreign = uploads / "user-kept"; foreign.mkdir(); (foreign / "keep.txt").write_text("keep", encoding="utf-8")
        odd = uploads / ("c" * 32); odd.mkdir(); (odd / "notes.txt").write_text("keep", encoding="utf-8")
        os.utime(odd, (aged, aged))
        target = tmp_path / "link-target"; target.mkdir(); (target / "0.part").write_bytes(b"keep")
        link = uploads / ("d" * 32)
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
            else:
                link.symlink_to(target, target_is_directory=True)
        except OSError:
            link = None
        stale = svc.init(mime="image/png", total_bytes=len(payload), sha256="", width=40, height=30)
        manifest = uploads / stale["upload_id"] / "manifest.json"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["created"] = time.time() - mod.UPLOAD_TTL_S - 10
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        (uploads / "../outside.txt").write_text("keep", encoding="utf-8")

        rebuilt = mod.UploadService(store, db, chunk_size=1024)
        assert rebuilt.missing_chunks(up["upload_id"], "device:phone") == list(range(1, up["expected_chunks"]))
        with pytest.raises(ValueError):
            rebuilt.missing_chunks(up["upload_id"], "device:other")
        for i in range(1, up["expected_chunks"]):
            rebuilt.put_chunk(up["upload_id"], i, payload[i * 1024:(i + 1) * 1024], "device:phone")
        meta = rebuilt.complete(up["upload_id"], "device:phone")
        assert store.get(meta["asset_id"]) == payload
        assert not (uploads / up["upload_id"]).exists()
        assert not legacy.exists()
        assert (fresh_legacy / "0.part").exists()
        assert (foreign / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert (odd / "notes.txt").exists()
        assert (target / "0.part").read_bytes() == b"keep"
        if link is not None:
            assert os.path.lexists(link)
        assert not (uploads / stale["upload_id"]).exists()
        with pytest.raises(ValueError):
            rebuilt.missing_chunks(stale["upload_id"])
        assert (store.root / "outside.txt").read_text(encoding="utf-8") == "keep"

        monkeypatch.setattr(mod, "MAX_RECOVERED_UPLOADS", 2)
        ids = [rebuilt.init(mime="image/png", total_bytes=len(payload), sha256="", width=40, height=30)["upload_id"]
               for _ in range(4)]
        bounded = mod.UploadService(store, db, chunk_size=1024)
        assert len([i for i in ids if i in bounded._uploads]) == 2
        assert len([p for p in uploads.iterdir() if mod.UploadService._owned_upload_dir(p) and (p / "manifest.json").exists()]) == 2
        assert (foreign / "keep.txt").exists() and (target / "0.part").exists()
    finally:
        db.conn.close()


def test_resolve_asset_refs_decodes_each_file_once(tmp_path, monkeypatch):
    from doubao_typeless.services import assets as mod
    from doubao_typeless.storage.asset_store import AssetStore
    store = AssetStore(tmp_path / "assets")
    meta = store.put_png(_png(), width=40, height=30, role="markup")
    calls = []
    real = mod.Image.open
    monkeypatch.setattr(mod.Image, "open", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = mod.resolve_asset_refs(store, [meta["asset_id"]])
    for _ in range(20):
        again = mod.resolve_asset_refs(store, [meta["asset_id"]])
    assert again == first and first[0]["role"] == "markup" and first[0]["width"] == 40
    assert len(calls) == 1
    again[0]["caption"] = "调用方修改不污染缓存"
    assert "caption" not in mod.resolve_asset_refs(store, [meta["asset_id"]])[0]
    path = store.root / f"{meta['asset_id']}.bin"
    time.sleep(0.01)
    path.write_bytes(_png(50, 30))
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10_000_000))
    assert mod.resolve_asset_refs(store, [meta["asset_id"]])[0]["width"] == 50
    path.unlink()
    with pytest.raises(ValueError):
        mod.resolve_asset_refs(store, [meta["asset_id"]])


# ---- 平台：无障碍角色、密钥迁移 ----

def test_terminal_and_code_roles_are_recognized():
    from doubao_typeless.platform.accessible import input_kind
    assert input_kind("terminal", "", editable=True) == "terminal"
    assert input_kind("Terminal", "Bash", editable=True) == "terminal"
    assert input_kind("text", "Monaco editor", editable=True) == "code"
    assert input_kind("entry", "Ask anything", editable=True) == "composer"
    assert input_kind("entry", "", editable=True) == "edit"
    assert input_kind("password text", "", editable=True) == "password"


def test_posix_keyring_migration_removes_plaintext(tmp_path, monkeypatch):
    from doubao_typeless.storage import secret_store as s
    saved = {}

    class FakeKeyring:
        def set_password(self, service, key, value):
            saved[key] = value

        def get_password(self, service, key):
            return saved.get(key)

    monkeypatch.setattr(s.sys, "platform", "linux")
    monkeypatch.setenv("DT_V3_SECRET_FILE", "1")
    assert s.put_secret(tmp_path, "review", "FAKE-TEST-KEY") == "file"
    plaintext = tmp_path / "secrets" / "review.txt"
    assert plaintext.is_file()
    monkeypatch.delenv("DT_V3_SECRET_FILE")
    monkeypatch.setattr(s, "_native_keyring", lambda: FakeKeyring())
    assert s.put_secret(tmp_path, "review", "FAKE-TEST-KEY") == "os"
    assert not plaintext.exists()
    assert s.get_secret(tmp_path, "review") == "FAKE-TEST-KEY"

    class Broken:
        def set_password(self, *args):
            raise RuntimeError("locked")
    monkeypatch.setenv("DT_V3_SECRET_FILE", "1")
    s.put_secret(tmp_path, "other", "FAKE-2")
    monkeypatch.delenv("DT_V3_SECRET_FILE")
    monkeypatch.setattr(s, "_native_keyring", lambda: Broken())
    assert s.put_secret(tmp_path, "other", "FAKE-3") == "memory"
    s._MEMORY.clear()


# ---- Qt：浮窗/审阅窗在连续更新下不积压、不抢滚动 ----

def _qt():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_hud_coalesces_cross_thread_updates_and_keeps_order():
    from PySide6.QtTest import QTest
    from doubao_typeless.ui.hud import HudController
    _qt()
    hud = HudController()
    hud.start()
    renders = []
    original = hud._apply_content_update
    hud._apply_content_update = lambda: (renders.append(hud.text), original())
    try:
        def speak():
            for i in range(200):
                hud.show_receiving("连续说话" + "字" * i)
        worker = threading.Thread(target=speak)
        worker.start(); worker.join()
        QTest.qWait(50)
        assert hud._body.toPlainText() == "连续说话" + "字" * 199
        assert len(renders) < 20, len(renders)  # 旧实现会排 200 次完整渲染
        # 顺序敏感：内容→隐藏→内容，最后必须可见并显示最新内容。
        def toggle():
            hud.show_receiving("A")
            hud.hide()
            hud.show_receiving("B")
        worker = threading.Thread(target=toggle)
        worker.start(); worker.join()
        QTest.qWait(50)
        assert hud._widget.isVisible() and hud._body.toPlainText() == "B"
    finally:
        hud.dismiss(); hud._widget.deleteLater(); _qt().processEvents()


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows desktop client")
def test_review_panel_live_update_keeps_reading_position(tmp_path):
    from PySide6.QtGui import QTextCursor
    from PySide6.QtTest import QTest
    from doubao_typeless.app import V3App
    from doubao_typeless.ui.desktop import ReviewPanel
    _qt()
    app = V3App(data_dir=tmp_path / "data", port=0)
    try:
        app.draft.text = "\n".join(f"第{i}行 审阅内容" for i in range(200))
        panel = ReviewPanel(app)
        panel.widget.show(); QTest.qWait(20)
        bar = panel.editor.verticalScrollBar()
        assert bar.maximum() > 0 and bar.value() == bar.maximum()  # 跟随最新
        bar.setValue(bar.maximum() // 3)
        reading = bar.value()
        app.draft.text += "\n手机又说了一句"
        panel.reload(); QTest.qWait(10)
        assert panel.editor.toPlainText().endswith("手机又说了一句")
        assert bar.value() == reading
        cursor = panel.editor.textCursor(); cursor.setPosition(0); cursor.setPosition(3, QTextCursor.KeepAnchor)
        panel.editor.setTextCursor(cursor)
        app.draft.text = "改写后的" + app.draft.text[3:]  # 非追加修改也保留选区与位置
        panel.reload(); QTest.qWait(10)
        assert panel.editor.textCursor().hasSelection() and not panel._editing and not app.review_editing
        cursor = panel.editor.textCursor(); cursor.clearSelection(); panel.editor.setTextCursor(cursor)
        bar.setValue(bar.maximum())
        app.draft.text += "\n最后一句"
        panel.reload(); QTest.qWait(10)
        assert bar.value() == bar.maximum()
        panel.widget.hide()
    finally:
        app.db.conn.close()


def test_hud_selection_survives_live_updates_with_emoji_and_defers_overlap():
    """选区在变化点之前：正文照常更新、选区按 UTF-16 位置不变；新内容会改到选中文字时暂缓并提示。"""
    from PySide6.QtTest import QTest
    from doubao_typeless.ui.hud import HudController
    _qt()
    hud = HudController()
    hud.start()
    try:
        old = "😀" * 30 + "需要保留选中" + "\n后面的正文" * 60
        hud.show_receiving(old); QTest.qWait(20)
        body = hud._body
        body.setTextCursor(body.document().find("需要保留选中"))
        bar = body.verticalScrollBar(); bar.setValue(0)
        for i in range(5):
            hud.show_receiving(old + "追加" * (i + 1))
        QTest.qWait(20)
        assert body.toPlainText() == old + "追加" * 5
        assert body.textCursor().selectedText() == "需要保留选中" and bar.value() == 0
        # 选区之后的非追加修改（改写尾部）同样显示。
        hud.show_receiving(old[:-3] + "改写"); QTest.qWait(10)
        assert body.toPlainText() == old[:-3] + "改写"
        assert body.textCursor().selectedText() == "需要保留选中"
        # 改到选中的文字：不覆盖选区，明确提示有新内容；回到最新后显示最新版。
        hud.show_receiving("全新的一段"); QTest.qWait(10)
        assert body.textCursor().selectedText() == "需要保留选中"
        assert hud._latest.isVisible() and "新内容" in hud._latest.text()
        hud._latest.click(); QTest.qWait(20)
        assert body.toPlainText() == "全新的一段" and not body.textCursor().hasSelection()
        assert "新内容" not in hud._latest.text()
    finally:
        hud.dismiss(); hud._widget.deleteLater(); _qt().processEvents()


def test_gui_identity_reads_do_not_wait_for_draft_disk_write(tmp_path, monkeypatch):
    """电脑写盘慢时，界面定时检查读到上一版完整身份而不等待；写完后读到新版。用户操作仍等待当前版。"""
    from doubao_typeless.app import V3App
    from doubao_typeless.storage import draft_snapshot
    app = V3App(data_dir=tmp_path / "data", port=0)
    entered, release = threading.Event(), threading.Event()
    original = draft_snapshot.save_draft

    def slow(*args, **kwargs):
        entered.set(); release.wait(5)
        return original(*args, **kwargs)

    payload = {"authority": "phone", "draft_id": "d", "epoch": "e", "generation": 0, "revision": 1,
               "text": "手机新的一版", "asset_refs": [], "asset_documents": [], "update_id": "u1",
               "_source": "remote", "_device_id": "p"}
    try:
        before = app.input_check_identity()
        monkeypatch.setattr(draft_snapshot, "save_draft", slow)
        worker = threading.Thread(target=lambda: app.apply_phone_update(payload))
        worker.start(); assert entered.wait(3)
        started = time.perf_counter()
        during = app.input_check_identity(wait=False)
        app.input_check_tick()
        assert time.perf_counter() - started < 0.1
        assert during == before  # 完整的上一版，不是新旧字段拼接
        release.set(); worker.join(5)
        after = app.input_check_identity(wait=False)
        assert after[2:] == (1, "手机新的一版") and after[:2] == ("d", "e")
    finally:
        release.set()
        app._commands.close(3); app.db.conn.close(); app._lock.release()


def test_blob_edits_are_never_assumed_equal(storage_page):
    page, _, _ = storage_page
    result = page.evaluate('''()=>{
      const d=png=>({schema:1,text:"t",revision:1,draft_id:"d",epoch:"e",saved_at:1,
        assets:[{id:"i",asset_id:"a",render_revision:1,caption:"",pending_png:png}]});
      const blob=new Blob(["AAAA"],{type:"image/png"});
      return {sameObject:DTStore.sameContent(d(blob),d(blob)),
        sameSizeType:DTStore.sameContent(d(blob),d(new Blob(["BBBB"],{type:"image/png"}))),
        clone:DTStore.sameContent(d(blob),d(new Blob(["AAAA"],{type:"image/png"})))};
    }''')
    # 内容无法同步比较：同一对象相等，其余一律视为不同（多留一份可恢复副本，不丢编辑）。
    assert result == {"sameObject": True, "sameSizeType": False, "clone": False}


def test_open_older_page_cannot_misread_payload_records(storage_page):
    """0.5.6 页面以数据库版本 1 打开：已开着的旧连接收到 versionchange 关闭，之后重开得到 VersionError，
    不会把 {$payload} 引用当成图片/场景读出再写回主稿；新页面仍读出完整内容。"""
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      const name="doubao-typeless-v3-drafts";
      const openV1=()=>new Promise((ok,bad)=>{const r=indexedDB.open(name,1);
        r.onupgradeneeded=()=>{if(!r.result.objectStoreNames.contains("drafts"))r.result.createObjectStore("drafts")};
        r.onsuccess=()=>ok(r.result);r.onerror=()=>bad(r.error)});
      // 旧页面：先以版本 1 打开并写入内联旧稿，连接一直开着（与 0.5.6 一样收到 versionchange 时关闭）。
      const legacy=await openV1();let closedByUpgrade=false;
      legacy.onversionchange=()=>{closedByUpgrade=true;legacy.close()};
      const scene="OLD:"+"a".repeat(5000);
      await new Promise((ok,bad)=>{const tx=legacy.transaction("drafts","readwrite");
        tx.objectStore("drafts").put({schema:1,text:"old",revision:1,draft_id:"d",epoch:"e",saved_at:1,
          assets:[{id:"i",asset_id:"a",render_revision:1,caption:"",scene}]},"current");tx.oncomplete=ok;tx.onerror=bad});
      const repo=new DTStore.DraftRepository(()=>{},()=>{},"new-tab");
      const migrated=await repo.claim();
      const next=structuredClone(migrated);next.text="new";next.assets[0].scene="NEW:"+"b".repeat(5000);
      repo.save(next);await repo.flush();
      let reopen="opened";try{(await openV1()).close()}catch(e){reopen=e.name}
      const loaded=await repo.load();
      return {migrated:migrated.assets[0].scene===scene,closedByUpgrade,reopen,
        final:loaded.assets[0].scene===next.assets[0].scene&&loaded.text==="new"};
    }''')
    assert result == {"migrated": True, "closedByUpgrade": True, "reopen": "VersionError", "final": True}
