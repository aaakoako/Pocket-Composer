"""Opus 5.5 审查修复：真实 Chromium IndexedDB 上的写入者接管与读失败（存储模块级，不是 Android 真机声明）。"""
from tests.test_v3_phone_storage import storage_page, storage_site  # noqa: F401  (pytest fixtures)

DRAFT = '{schema:1,text:"A 页的稿",revision:1,draft_id:"d",epoch:"e",assets:[],saved_at:1}'


def test_superseded_tab_cannot_overwrite_current_and_keeps_side_copy(storage_page):
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      const sup={a:0,b:0};
      const a=new DTStore.DraftRepository(()=>{},()=>sup.a++,"tab-a");
      const b=new DTStore.DraftRepository(()=>{},()=>sup.b++,"tab-b");
      await a.claim(); a.save(%s); await a.flush();
      const seen=await b.claim();
      b.save({...seen,text:"B 页的新稿",revision:2}); await b.flush();
      a.save({...seen,text:"A 页迟到的旧内容",revision:2});
      let error=""; try{await a.flush()}catch(e){error=e.message}
      a.save({...seen,text:"A 页迟到的旧内容 2",revision:3});
      try{await a.flush()}catch{}
      const current=await b.load();
      const sides=await b.sideDrafts();
      return {seen:seen.text,error,current:current.text,sup,aWriter:await a.isWriter(),bWriter:await b.isWriter(),
              sides:sides.map(s=>[s.key,s.draft.text])};
    }''' % DRAFT)
    assert result["seen"] == "A 页的稿"
    assert result["error"] == "DRAFT_SUPERSEDED" and result["current"] == "B 页的新稿"
    assert result["sup"] == {"a": 1, "b": 0}
    assert result["aWriter"] is False and result["bWriter"] is True
    assert result["sides"] == [["side-superseded-tab-a", "A 页迟到的旧内容 2"]]


def test_reclaiming_tab_reads_latest_and_resumes_writing(storage_page):
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      const a=new DTStore.DraftRepository(()=>{},()=>{},"tab-a");
      const b=new DTStore.DraftRepository(()=>{},()=>{},"tab-b");
      await a.claim(); a.save(%s); await a.flush();
      await b.claim(); b.save({...(await b.load()),text:"B 写的",revision:2}); await b.flush();
      const latest=await a.claim();
      a.save({...latest,text:"A 接着写",revision:3}); await a.flush();
      let bError=""; b.save({...latest,text:"B 已暂停",revision:4}); try{await b.flush()}catch(e){bError=e.message}
      // 与当前主稿相同的被拒快照不另存
      b.save({...(await a.load())}); try{await b.flush()}catch{}
      return {latest:latest.text,current:(await a.load()).text,bError,
              sides:(await a.sideDrafts()).map(s=>s.draft.text)};
    }''' % DRAFT)
    assert result == {"latest": "B 写的", "current": "A 接着写", "bError": "DRAFT_SUPERSEDED",
                      "sides": ["B 已暂停"]}


def test_backup_and_replace_are_also_guarded(storage_page):
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      const a=new DTStore.DraftRepository(()=>{},()=>{},"tab-a");
      const b=new DTStore.DraftRepository(()=>{},()=>{},"tab-b");
      await a.claim(); a.save(%s); await a.flush(); await b.claim();
      const d=await a.load(); const errors=[];
      try{await a.replaceWithBackup(d,{...d,text:"",epoch:"new"})}catch(e){errors.push(e.message)}
      try{await a.backup({...d,text:"x"})}catch(e){errors.push(e.message)}
      return {errors,current:(await b.load()).text,backup:await b.load("before-replace")};
    }''' % DRAFT)
    assert result == {"errors": ["DRAFT_SUPERSEDED", "DRAFT_SUPERSEDED"], "current": "A 页的稿", "backup": None}


def test_read_failure_is_an_error_not_an_empty_draft(storage_page):
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      const a=new DTStore.DraftRepository(()=>{},()=>{},"tab-a");
      await a.claim(); a.save(%s); await a.flush();
      const original=IDBObjectStore.prototype.get;
      IDBObjectStore.prototype.get=function(key){const r=original.call(this,key);
        if(this.name==="drafts"&&key==="current")this.transaction.abort();return r};
      const b=new DTStore.DraftRepository(()=>{},()=>{},"tab-b");
      let error=""; try{await b.claim()}catch(e){error=e.message}
      // 用户选择「先写新稿」：只写独立记录，主稿不动。
      b.useSideKey(); b.save({schema:1,text:"读不出时先写的新稿",revision:1,draft_id:"n",epoch:"n",assets:[],saved_at:2});
      await b.flush();
      IDBObjectStore.prototype.get=original;
      return {error,current:(await a.load()).text,writer:await a.isWriter(),
              sides:(await a.sideDrafts()).map(s=>[s.key.startsWith("side-new-"),s.draft.text])};
    }''' % DRAFT)
    assert result["error"] == "LOCAL_STORAGE_READ_FAILED"
    assert result["current"] == "A 页的稿" and result["writer"] is True
    assert result["sides"] == [[True, "读不出时先写的新稿"]]


def test_unrecognised_current_record_is_preserved_before_claim(storage_page):
    page, _, _ = storage_page
    result = page.evaluate('''async()=>{
      await new Promise((ok,bad)=>{const r=indexedDB.open("doubao-typeless-v3-drafts",1);
        r.onupgradeneeded=()=>r.result.createObjectStore("drafts");
        r.onsuccess=()=>{const tx=r.result.transaction("drafts","readwrite");
          tx.objectStore("drafts").put({schema:9,body:"未来版本的稿"},"current");
          tx.oncomplete=()=>{r.result.close();ok()};tx.onerror=bad}});
      const a=new DTStore.DraftRepository(()=>{},()=>{},"tab-a");
      const saved=await a.claim();
      const keys=await new Promise(ok=>{const r=indexedDB.open("doubao-typeless-v3-drafts",1);
        r.onsuccess=()=>{const q=r.result.transaction("drafts").objectStore("drafts").getAllKeys();
          q.onsuccess=()=>{r.result.close();ok(q.result)}}});
      return {saved,unreadable:keys.filter(k=>String(k).startsWith("unreadable-")).length};
    }''')
    assert result == {"saved": None, "unreadable": 1}
