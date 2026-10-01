import { decorateTools } from "./icons";
import { installInputMotion } from './input-motion';
import { overlayHistory } from "./overlay-history";
import { SharedEditor, type Tool } from "./editor/canvas";
import { applyReady, applyRotated, buildDraftUpdate, buildPrimaryUpdate, rotatePrimary, DraftOutbox } from "./sync.js";
import { looksLikeKeyScript, newId } from "./transport/protocol";
import { uploadPng, type UploadTicket } from "./transport/upload";
import { DraftRepository, type SavedDraft } from "./storage/drafts";

type Session = {
  session_id: string;
  token: string;
  device_id?: string;
  allow_insert?: boolean;
  allow_capture?: boolean;
};
type AssetStatus = "queued" | "editing" | "ready" | "failed";
type Asset = {
  id: string;
  kind: string;
  preview: string;
  asset_id?: string;
  w?: number;
  h?: number;
  scene?: string;
  source?: string;
  caption?: string;
  status?: AssetStatus;
  render_revision?: number;
  pending_png?: Blob;
  upload_ticket?: UploadTicket;
  progress?: number;
  // Saved before editing; unsaved scratch never replaces the committed image.
  edit_original?: Asset | null;
  edit_scene?: string;
  edit_caption?: string;
  edit_text?: {value: string; x: number; y: number};
};

export function boot(root: HTMLElement): void {
  root.innerHTML = `
    <div class="page">
      <header class="head" id="mobileHead">
        <div class="pc-name"><i class="dot" id="connDot"></i>Pocket Composer<small id="connText">正在连接电脑</small></div>
        <button id="connectBtn">连接</button>
        <button id="historyBtn" aria-label="最近图文">最近</button>
        <button id="settingsBtn" aria-label="设置">设置</button>
      </header>
      <section class="composer" id="composer">
        <div class="composer-heading"><span>这次想做什么？</span><i id="inputActivity" class="input-activity" aria-hidden="true" title="正在输入" hidden><b></b><b></b><b></b><b></b></i><button id="clearDraft" aria-label="清空当前文字和图片">清空</button></div>
        <div class="attach" id="attachments"></div>
        <div id="removeNotice" class="undo-notice" role="status" hidden><span id="removeMessage"></span><button id="undoRemove">撤销删除</button></div>
        <div id="conflictBanner" class="conflict" hidden>
          <b>手机和电脑有不同的草稿</b><p>两份内容都还在，请选择这次继续使用哪一份。</p>
          <button id="useLocal">继续手机这份</button><button id="useServer">采用电脑这份</button>
        </div>
        <textarea id="text" placeholder="点这里，用手机输入法说话…&#10;&#10;也可以圈出问题，或画个草图。" aria-label="本次图文说明"></textarea>
        <div class="writehint"><span>用手机输入法说话，文字自动同步</span><span id="charCount">0 字</span></div>
        <div class="writehint"><span id="captionHint"></span></div>
        <div class="tools">
          <button id="captureBtn">截电脑</button>
          <button id="photoBtn">相册</button>
          <button id="boardBtn">白板</button>
        </div>
        <button class="primary" id="sendBtn" disabled>插入电脑</button>
        <section id="submitPanel" class="submit-panel" hidden>
          <p>上一份已放入电脑输入框。请看电脑确认图文完整。</p>
          <button id="submitMessage">确认后发送上一份</button>
        </section>
        <div class="sync-map" id="syncMap" data-state="offline" aria-hidden="true">
          <svg viewBox="0 0 24 24"><rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18h4"/></svg>
          <span class="sync-track"><i></i></span>
          <svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 22h8M12 17v5"/></svg>
          <span class="sync-mark"></span>
        </div>
        <p id="deliveryStatus" class="progress-detail" role="status" aria-live="polite" hidden></p><p id="transferStatus" role="status" aria-live="polite">手机主稿 · 等待同步</p><p id="sync">图在前，文字在后 · 不自动发送</p><p id="localSave" role="status" aria-live="polite"></p>
      </section>
      <section class="editor" id="editor">
        <div class="head">
          <button id="back" aria-label="取消编辑">取消</button>
          <b id="editTitle">图片标注</b>
          <button id="undoBtn" aria-label="撤销">撤销</button>
          <button id="redoBtn" aria-label="重做">重做</button>
          <button class="primary" id="done">保存图片</button>
        </div>
        <div id="canvasTextPanel" class="canvas-text-panel" hidden>
          <label for="canvasTextInput">画布文字</label><textarea id="canvasTextInput" rows="2" placeholder="输入要标注的文字"></textarea>
          <div><button id="canvasTextCancel">取消文字</button><button id="canvasTextAdd" class="primary">放入画布</button></div>
        </div>
        <p id="editorStatus" role="status" aria-live="polite" hidden></p><div id="stage"></div>
        <div class="crop-actions" id="cropActions">
          <button id="cropReset">全图</button>
          <button id="cropApply">应用裁剪</button>
        </div>
        <div class="palette">
          <button data-color="#D45243" class="swatch selected" style="background:#D45243"></button>
          <button data-color="#326AC5" class="swatch" style="background:#326AC5"></button>
          <button data-color="#5B5CE2" class="swatch" style="background:#5B5CE2"></button>
          <button data-color="#202437" class="swatch" style="background:#202437"></button>
          <button id="widthBtn">中 · 5</button>
        </div>
        <div class="palette">
          <button data-tool="pen" class="selected">画笔</button>
          <button data-tool="arrow">箭头</button>
          <button data-tool="rect">方框</button>
          <button data-tool="number">编号</button>
          <button data-tool="crop">裁剪</button>
          <button id="moreBtn">更多</button>
        </div>
        <div class="palette extra" id="extra">
          <button data-tool="marker">记号笔</button>
          <button data-tool="highlight">荧光笔</button>
          <button data-tool="ellipse">椭圆</button>
          <button data-tool="line">直线</button>
          <button data-tool="text">文字</button>
          <button data-tool="eraser">橡皮</button>
          <button data-tool="mask">遮挡</button>
          <button data-tool="select">选择</button>
          <button id="wire">线框按钮</button>
        </div>
        <button id="captionToggle" aria-expanded="false">图注 · 补充说明</button>
        <div class="caption-drawer" id="captionDrawer" hidden>
          <textarea id="captionInput" placeholder="给这张图补一句说明，不发Enter"></textarea>
        </div>
      </section>
    </div>
    <input id="file" type="file" accept="image/png,image/jpeg,image/webp" multiple hidden />
    <div class="sheet" id="sheet"><div class="card" role="dialog" aria-modal="true" aria-label="操作面板" id="sheetDialog"><div class="sheet-toolbar"><button id="closeSheet" aria-label="关闭面板">关闭</button></div><div id="sheetCard"></div></div></div>
  `;

  const state = {
    text: "",
    revision: 0,
    generation: 0,
    draft_id: "",
    epoch: "",
    assets: [] as Asset[],
    session: null as Session | null,
    online: false,
    uploading: false,
    editorKind: "图片标注",
    removed: [] as {asset: Asset; index: number; epoch: string}[],
    conflict: null as any,
    sending: false,
    last_intent: null as {signature:string;id:string} | null,
  };
  let ws: WebSocket | null = null;
  // One in-flight UI operation; its durable intent remains in last_intent for retries.
  let activeSend: {intentId: string; timer: number} | null = null;
  let clearingDraft = false;
  function finishSend(operation: typeof activeSend): boolean {
    if (!operation || activeSend !== operation) return false;
    window.clearTimeout(operation.timer);
    activeSend = null;
    state.sending = false;
    return true;
  }
  function finishFeedback(msg: any): boolean {
    return !!activeSend?.intentId && msg.intent_id === activeSend.intentId && finishSend(activeSend);
  }
  decorateTools(root);
  let editor: SharedEditor | null = null;
  let textPoint: {x:number;y:number} | null = null;
  function closeCanvasText() {
    $("canvasTextPanel").hidden = true;
    textPoint = null;
  }
  function chooseTool(tool: Tool) {
    if (editor) editor.tool = tool;
    root.querySelectorAll<HTMLElement>("[data-tool]").forEach(b => {
      b.classList.toggle("selected", b.dataset.tool === tool);
      b.setAttribute("aria-pressed", String(b.dataset.tool === tool));
    });
    $("cropActions").classList.toggle("show", tool === "crop");
  }
  let currentId = "";
  const blobUrls: string[] = [];
  let reconnectTimer = 0;
  let resumeFlight: Promise<boolean> | null = null;
  let resumePending = false;
  let secretRetryTimer = 0;
  let closingForAuth = false;
  let sheetRevision = 0;
  let sessionReady = false;
  let editorStartScene = "";
  let editorLoading = false;
  let editorSequence = 0;
  let editorAbort: AbortController | null = null;
  let submitBusy = false;
  let submitAvailable = false;
  let pendingCapture = "";
  const receivedCaptures = new Set<string>();

  function rememberBlob(url: string): string {
    if (url.startsWith("blob:")) blobUrls.push(url);
    return url;
  }

  function forgetBlobs(): void {
    while (blobUrls.length) {
      URL.revokeObjectURL(blobUrls.pop()!);
    }
  }
  const $ = (id: string) => document.getElementById(id)!;
  const inputMotion=installInputMotion($('text') as HTMLTextAreaElement,$('inputActivity'));
  let sheetReturnFocus: HTMLElement | null = null;
  function openSheet() {
    if (!$("sheet").classList.contains("show")) sheetReturnFocus = document.activeElement as HTMLElement;
    $("sheet").classList.add("show");
    root.querySelector<HTMLElement>(".page")!.inert = true;
    $("sheetDialog").setAttribute("aria-label", $("sheetCard").querySelector("h2,h3")?.textContent || "操作面板");
    $("closeSheet").focus({preventScroll:true});
  }
  function closeSheet() {
    ++sheetRevision; // Closed panels cannot be rewritten by a late network response.
    $("sheet").classList.remove("show");
    root.querySelector<HTMLElement>(".page")!.inert = false;
    if (sheetReturnFocus?.isConnected && sheetReturnFocus.getClientRects().length) sheetReturnFocus.focus({preventScroll:true});
    sheetReturnFocus = null;
  }
  $("closeSheet").onclick = closeSheet;
  const syncOverlayHistory = overlayHistory(
    () => $("sheet").classList.contains("show") || editorOpen(),
    () => {if ($("sheet").classList.contains("show")) closeSheet(); else cancelEditor();}
  );
  const overlayObserver = new MutationObserver(syncOverlayHistory);
  for (const id of ["sheet", "editor"]) overlayObserver.observe($(id), {attributes:true, attributeFilter:["class"]});
  const resizeViewport = () => {
    const height = window.visualViewport?.height ?? window.innerHeight;
    root.style.setProperty("--visible-height", `${height}px`);
    root.classList.toggle("compact-keyboard", height < 520);
  };
  window.visualViewport?.addEventListener("resize", resizeViewport);
  window.addEventListener("resize", resizeViewport);
  resizeViewport();
  $("canvasTextCancel").onclick = () => {closeCanvasText(); chooseTool("pen"); saveOpenEditor();};
  function commitCanvasText(): boolean {
    if (!editor || !textPoint) return false;
    const value = ($("canvasTextInput") as HTMLTextAreaElement).value;
    if (!value.trim()) return false;
    editor.addText(value, textPoint);
    closeCanvasText(); chooseTool("select"); saveOpenEditor();
    return true;
  }
  $("canvasTextAdd").onclick = () => {if (!commitCanvasText()) $("canvasTextInput").focus();};
  const headers = (): Record<string, string> =>
    state.session
      ? { "X-DT-Session": state.session.session_id, "X-DT-Token": state.session.token }
      : {};

  function toast(t: string) {
    $("sync").textContent = t;
    if($("editor").classList.contains("show")){$("editorStatus").hidden=false;$("editorStatus").textContent=t;}
  }

  const DRAFT_KEY = "dt.v3.draft";
  let restored = false;
  let persistTimer = 0;
  const repository = new DraftRepository(status => {
    $("localSave").textContent = status === "saved" ? "草稿已保存在这台手机" :
      status === "saving" ? "正在保存手机草稿…" : "手机存储不可用；请先发送或保留此页面，勿直接关闭";
  });

  function draftSnapshot(): SavedDraft {
    return {schema: 1, text: state.text, revision: state.revision,
      draft_id: state.draft_id, epoch: state.epoch, generation: state.generation, saved_at: Date.now(),
      assets: state.assets.map(a => ({...a})), last_intent:state.last_intent};
  }

  function persistDraft() {
    if (!restored) return;
    window.clearTimeout(persistTimer);
    persistTimer = window.setTimeout(() => repository.save(draftSnapshot()), 120);
  }

  async function restoreDraft() {
    let saved: any = null;
    try { saved = await repository.load(); }
    catch { $("localSave").textContent = "无法读取手机存储；已有内容不会被清除"; }
    if (!saved) {
      // 只迁移旧记录；成功保存之前不删除旧备份。
      try { saved = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "null"); } catch {}
    }
    if (saved && typeof saved === "object") {
      state.text = String(saved.text || "");
      state.revision = Number(saved.revision || 0);
      state.draft_id = String(saved.draft_id || "");
      state.epoch = String(saved.epoch || "");
      state.generation = Number(saved.generation || 0);
      state.last_intent = saved.last_intent && typeof saved.last_intent.id === "string" ? saved.last_intent : null;
      state.assets = Array.isArray(saved.assets) ? saved.assets.slice(0,6) : [];
      let recoveredEditing = false;
      for (const a of state.assets) {
        if (!a.id || typeof a.preview !== "string") {a.id ||= newId(); a.preview = ""; a.status = "failed"; recoveredEditing = true;}
        if (a.status === "editing") {a.status = "failed"; recoveredEditing = true;}
      }
      // A recovered editor is a new asset state; reusing its old revision causes a false conflict.
      if (recoveredEditing) {state.revision++; state.last_intent = null;}
    }
    state.draft_id ||= newId();
    state.epoch ||= newId();
    restored = true;
  }

  function sendLabel(): string {
    if (!state.online || !state.session) return "未连接";
    if (state.uploading) return "图片正在同步，文字已保留";
    if (state.sending) return "正在确认当前图文…";
    if (state.assets.some(a => !a.asset_id || a.status !== "ready")) return "请先完成图片";
    if (!state.text.trim() && !state.assets.length) return "插入电脑";
    return state.assets.length ? `插入 ${state.assets.length} 张图${state.text.trim() ? "和文字" : ""}` : "插入并复制";
  }

  let attachmentsSignature = "";
  function update() {
    renderSyncState();
    const input = $("text") as HTMLTextAreaElement;
    if (input.value !== state.text) input.value = state.text;
    $("charCount").textContent = `${[...state.text].length} 字`;
    $("captionHint").textContent = state.assets.map((a, i) => `${i + 1}·${a.kind}`).join(" ");
    $("connText").textContent = state.online ? "已连接电脑" : resumePending ? "自动续接中 · 可继续写" : "离线也可继续写";
    $("connDot").style.background = state.online ? "#5B5CE2" : "#858DA0";
    $("connectBtn").hidden = state.online;
    const btn = $("sendBtn") as HTMLButtonElement;
    ($("clearDraft") as HTMLButtonElement).disabled = !restored || clearingDraft || finishingEditor ||
      (!state.text && !state.assets.length && !state.sending && !state.conflict && !pendingCapture);
    btn.textContent = sendLabel();
    btn.disabled = !state.online || !state.session || state.uploading || state.sending || !!state.conflict ||
      state.assets.some(a => !a.asset_id || (a.status && a.status !== "ready")) || (!state.text.trim() && !state.assets.length);
    $("conflictBanner").hidden = !state.conflict;
    state.removed = state.removed.filter(item => item.epoch === state.epoch);
    $("removeNotice").hidden = !state.removed.length;
    $("removeMessage").textContent = state.removed.length ? `已移除${state.removed[state.removed.length-1].asset.kind}` : "";
    const signature = JSON.stringify(state.assets.map(a => [a.id,a.asset_id,a.status,a.render_revision,a.preview,a.progress]));
    if (signature === attachmentsSignature) return;
    attachmentsSignature = signature;
    const strip = $("attachments");
    const previousIds = new Set(Array.from(strip.children).map(el => (el as HTMLElement).dataset.assetId));
    strip.replaceChildren();
    if (!state.assets.length) {
      strip.classList.add("empty");
      return;
    }
    strip.classList.remove("empty");
    state.assets.forEach((a, i) => {
      const wrap = document.createElement("div");
      wrap.className = "attach-card";
      wrap.dataset.assetId = a.id;
      if (!previousIds.has(a.id)) wrap.classList.add('arriving');
      const status = a.pending_png && a.status !== "ready" ? (a.status === "failed" ? "等待重试" : `同步 ${a.progress || 0}%`) :
        a.status === "queued" ? "待编辑" : a.status === "editing" ? "编辑中" : a.status === "failed" ? "未传完" : "电脑已收到";
      wrap.innerHTML = `<img alt="${i + 1} · ${a.kind}" /><label>${i + 1} · ${a.kind} · ${status}</label><button class="left">←</button><button class="right">→</button><button class="remove">删</button>`;
      if (a.pending_png && a.status !== 'ready') {
        const progress = document.createElement('progress');
        progress.className = 'asset-progress'; progress.max = 100;
        progress.value = Math.max(0, Math.min(100, a.progress || 0));
        progress.setAttribute('aria-label', `第 ${i+1} 张图片上传进度`);
        wrap.appendChild(progress);
      }
      wrap.dataset.state = a.asset_id && (!a.status || a.status === 'ready') ? 'ready' : (a.status || 'queued');
      const img = wrap.querySelector("img") as HTMLImageElement;
      if (a.preview.startsWith("/v3/assets/") && state.session) {
        void fetch(a.preview, {headers: headers()}).then(async res => {
          if (!res.ok) return;
          const url = URL.createObjectURL(await res.blob());
          img.onload = img.onerror = () => URL.revokeObjectURL(url);
          img.src = url;
        }).catch(() => {});
      } else img.src = a.preview;
      img.addEventListener("click", () => {
        void openEditor(a.kind === "白板" ? "快速白板" : "图片标注", a).catch(editorFailure);
      });
      wrap.querySelector(".left")!.addEventListener("click", (ev) => {
        ev.stopPropagation();
        move(i, -1);
      });
      wrap.querySelector(".right")!.addEventListener("click", () => move(i, 1));
      wrap.querySelector(".remove")!.addEventListener("click", () => removeAt(i));
      const left = wrap.querySelector<HTMLButtonElement>(".left")!;
      const right = wrap.querySelector<HTMLButtonElement>(".right")!;
      left.disabled = i === 0; right.disabled = i === state.assets.length - 1;
      left.setAttribute("aria-label", `前移第 ${i+1} 张图片`);
      right.setAttribute("aria-label", `后移第 ${i+1} 张图片`);
      wrap.querySelector(".remove")!.setAttribute("aria-label", `删除第 ${i+1} 张图片`);
      if (a.pending_png && a.status === "failed") {
        const retry = document.createElement("button"); retry.textContent = "重试上传";
        retry.className = "retry";
        retry.onclick = () => {a.status = "queued"; sendDraft(); update(); void uploadPending();};
        wrap.append(retry);
      }
      strip.append(wrap);
    });
  }

  function move(i: number, dir: number) {
    const j = i + dir;
    if (j < 0 || j >= state.assets.length) return;
    const copy = state.assets.splice(i, 1)[0];
    state.assets.splice(j, 0, copy);
    sendDraft();
    update();
  }

  function removeAt(i: number) {
    const removed = state.assets.splice(i, 1)[0];
    uploadControllers.get(removed.id)?.abort();
    state.removed.push({asset:removed, index:i, epoch:state.epoch});
    sendDraft();
    update();
  }

  $("undoRemove").onclick = () => {
    if (state.assets.length >= 6) {toast("本次已有六张图片，请先移除一张再撤销"); return;}
    const item = state.removed.pop();
    if (!item || item.epoch !== state.epoch) {update(); return;}
    state.assets.splice(Math.min(item.index, state.assets.length), 0, item.asset);
    sendDraft(); update(); void uploadPending();
  };

  function connect() {
    if (!state.session || !navigator.onLine) return;
    window.clearTimeout(reconnectTimer);
    if (ws && ws.readyState <= 1) return;
    closingForAuth = false;
    sessionReady = false;
    const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws = socket;
    ws.onopen = () => {
      state.online = false;
      update();
      ws!.send(JSON.stringify({ protocol: 3, type: "session.hello", session_id: state.session?.session_id, token: state.session?.token }));
    };
    ws.onclose = () => {
      if (ws !== socket) return;
      state.online = false;
      finishSend(activeSend);
      submitAvailable = false; $("submitPanel").hidden = true;
      sessionReady = false;
      outbox.disconnect();
      for (const controller of uploadControllers.values()) controller.abort();
      ws = null;
      update();
      if (!closingForAuth && state.session) reconnectTimer = window.setTimeout(connect, 1500);
    };
    ws.onmessage = (ev) => {
      if (ws !== socket) return;
      lastPong = Date.now();
      let msg: any;
      try {msg = JSON.parse(ev.data);} catch {return;}
      if (looksLikeKeyScript(msg)) return;
      if (msg.type === "session.ready") {
        state.online = true;
        sessionReady = true;
        void pullRememberedSecret();
        if (!(msg.capabilities || []).includes("phone-primary-v1")) {
          toast("电脑版本较旧，请使用这份新版客户端；手机稿已保留"); return;
        }
        outbox.disconnect();
        // 先处理上次可能丢失的完成回执，再同步本地最新版本。不采纳电脑旧镜像。
        void reconcileReceipt().finally(() => {if (ws === socket && sessionReady && !clearingDraft) {publishCurrent();outbox.connect();void uploadPending();}});
        update();
      }
      if (msg.type === "draft.prepare") {
        void prepareForDesktop(String(msg.request_id || ""));
      }
      if (msg.type === "device.remembered") {
        // 使用可重取的认证接口；只有写入手机存储后才确认已记住。
        void pullRememberedSecret();
      }
      if (msg.type === "draft.ack") {
        if (msg.error) outbox.reject(msg);
        else outbox.acknowledge(msg);
      }
      if (msg.type === "delivery.progress") {
        if (activeSend && (!activeSend.intentId || msg.intent_id !== activeSend.intentId)) return;
        const detail = msg.stage === "text" ? "图片粘贴已发出，正在插入文字…" :
          `第 ${msg.index} / ${msg.total} 张图片${msg.stage === "image_wait" ? "粘贴已发出，正在继续" : "正在插入"}…`;
        $("deliveryStatus").textContent = `电脑正在处理已提交的图文：${detail}`; $("deliveryStatus").hidden = false; update();
      }
      if (msg.type === "attempt.status") {
        if (activeSend && !finishFeedback(msg)) return;
        const labels: Record<string, string> = {CONFIRMED:"已放入输入框",UNKNOWN:"已尝试插入，可召回重试",NO_STEPS:"未插入，请先选中电脑输入框",PARTIAL:"只完成一部分，请查看恢复选项",BUSY:"正在处理上一份内容"};
        const detail = msg.progress?.message || labels[msg.result] || "插入未完成，内容保留";
        $("deliveryStatus").textContent=`上次插入：${detail}`;$("deliveryStatus").hidden=false;
        toast(`上次插入：${detail}`);
        update();
      }
      if (msg.type === "draft.rotated") {
        const relevant = !activeSend || (!!activeSend.intentId && msg.intent_id === activeSend.intentId);
        finishFeedback(msg); update();
        if(relevant && msg.progress?.message){$("deliveryStatus").textContent=`上次插入：${msg.progress.message}`;$("deliveryStatus").hidden=false;}
        void handleReceipt(msg);
      }
      if (msg.type === "draft.restore_proposal") {
        showRestoreProposal(msg);
      }
      if (msg.type === "recall.ready") toast(msg.text_unchanged ? "已召回上次待插入，当前草稿未改" : "召回异常");
      if (msg.type === "error") {
        const relevant = !activeSend || (!!activeSend.intentId && msg.intent_id === activeSend.intentId);
        finishFeedback(msg);
        update();
        outbox.reject(msg);
        const err = String(msg.error || "");
        if (["OTHER_PHONE_OWNER","PHONE_IDENTITY_CONFLICT","PHONE_REVISION_CONFLICT","STALE_PHONE_REVISION","STALE_PHONE_GENERATION"].includes(err)) {
          state.conflict = msg.mirror;
          $("conflictBanner").querySelector("b")!.textContent = err === "OTHER_PHONE_OWNER" ? "另一台手机正在编辑" : "发现另一份编辑记录";
          $("conflictBanner").querySelector("p")!.textContent = "要改由这台手机继续吗？另一份稿会保存在电脑恢复记录中。";
          $("useServer").hidden = true;
          update(); return;
        }
        if (/session revoked|session expired|bad token/i.test(err)) {
          sessionStorage.removeItem("dt.v3.session");
          state.session = null;
          closingForAuth = true;
          state.online = false; sessionReady = false; outbox.disconnect();
          finishSend(activeSend);
          ws?.close();
          ws = null;
          update();
          void recoverConnection(true);
          return;
        }
        if (relevant) toast(err.includes("not granted") || err === "CAPTURE_DENIED" ? "这台手机还没有截图权限，文字仍可同步" : err);
        return;
      }
      if (msg.type === "capture.result") {
        if (clearingDraft) return;
        // 过期请求不能冒充后来一次截图，也不能重新打开已经取消的编辑流程。
        if (msg.request_id && msg.request_id !== pendingCapture) return;
        pendingCapture = ""; ($("captureBtn") as HTMLButtonElement).disabled = false;
        if (msg.error) {
          toast(String(msg.message || msg.error).includes("CAPTURE") || String(msg.error).includes("not granted")
            ? "这台手机还没有截图权限，文字仍可同步"
            : String(msg.message || msg.error));
          return;
        }
        if (!msg.asset?.asset_id || state.assets.length >= 6 || receivedCaptures.has(msg.asset.asset_id)) return;
        receivedCaptures.add(msg.asset.asset_id);
        const a: Asset = {
          id: msg.asset.asset_id,
          asset_id: msg.asset.asset_id,
          kind: "截图",
          preview: `/v3/assets/${msg.asset.asset_id}`,
          w: msg.asset.width,
          h: msg.asset.height,
        };
        a.status = "queued";
        a.source = a.preview;
        state.assets.push(a);
        sendDraft();
        update();
        void openNextQueued();
      }
    };
  }

  function assetRefs(): string[] {
    const unfinished = state.assets.filter((a) => !a.asset_id || (a.status && a.status !== "ready"));
    if (unfinished.length) {
      throw new Error("incomplete assets");
    }
    return state.assets.map((a) => a.asset_id as string);
  }

  function documents() {
    return state.assets.map(a => ({id: a.id, asset_id: a.asset_id || "", status: a.status || "ready",
      render_revision: a.render_revision || 1, caption: a.caption || ""}));
  }

  let latestMessage: any = null;
  let syncState = "offline";
  let lastPong = Date.now();
  let receiptChain: Promise<void> = Promise.resolve();
  const outbox = new DraftOutbox({debounceMs:100,
    send: (message: any) => {
      if (!ws || ws.readyState !== WebSocket.OPEN || !sessionReady || ws.bufferedAmount > 128*1024)
        throw new Error("BACKPRESSURE");
      ws.send(JSON.stringify(message));
    },
    persist: async () => {
      window.clearTimeout(persistTimer);
      repository.save(draftSnapshot()); await repository.flush();
    },
    onState: (status: string) => {
      syncState = status;
      renderSyncState();
    },
  });
  function renderSyncState() {
    $("transferStatus").dataset.transport = syncState;
    let status = syncState;
    if (status === 'synced') {
      if (state.assets.some(a => a.status === 'failed')) status = 'asset_failed';
      else if (state.assets.some(a => a.pending_png && a.status !== 'ready')) status = 'uploading';
      else if (state.assets.some(a => !a.asset_id || a.status === 'editing')) status = 'editing';
    }
    const labels: Record<string,string> = {uploading:"图片仍在传输，电脑尚未收到全部图文",asset_failed:"图片上传失败，原稿保留，可重试",editing:"图片尚未保存到本次图文",offline:"手机已保留 · 连接恢复后继续同步",synced:"电脑已收到当前版本 · 不自动发送",
      syncing:"正在同步最新图文…",retrying:"网络较慢，正在补发当前稿…",conflict:"同步暂停，手机稿保留；请勿同时打开两个编辑页",save_failed:"手机存储暂不可写，内容留在页面中"};
    const shortLabels: Record<string,string> = {uploading:"图片传输中",asset_failed:"图片待重试",editing:"图片待保存",offline:"离线 · 稿件保留",synced:"电脑已收到当前版本",
      syncing:"同步中",retrying:"正在补发",conflict:"草稿有分歧",save_failed:"本地保存失败"};
    $("transferStatus").textContent = shortLabels[status] || status;
    $("transferStatus").title = labels[status] || status;
    $("transferStatus").setAttribute('aria-label',labels[status] || status);
    $("syncMap").dataset.state = status;
    $("transferStatus").dataset.busy = String(['syncing','retrying','uploading'].includes(status));
  }
  function currentMessage() {
    if (!latestMessage || latestMessage.epoch !== state.epoch || latestMessage.revision !== state.revision)
      latestMessage = {...buildDraftUpdate({...state, revision:state.revision - 1}), authority:"phone",generation:state.generation,update_id:newId()};
    return latestMessage;
  }
  function publishCurrent() {
    if (clearingDraft) return;
    outbox.offer(currentMessage());
  }
  function sendDraft() {
    if (clearingDraft) return;
    // New authoring cancels an old send affordance; rotation fetches a fresh status afterwards.
    submitAvailable = false; $("submitPanel").hidden = true;
    // Feedback from an earlier insertion must never describe the newly edited draft.
    $("deliveryStatus").hidden = true; $("deliveryStatus").textContent = "";
    $("sync").textContent = "图在前，文字在后 · 不自动发送";
    latestMessage = buildPrimaryUpdate(state, newId());
    persistDraft();
    if (restored) publishCurrent();
  }
  async function handleReceiptNow(msg: any) {
    if (!msg.phone_primary) return;
    // 写下上一份可恢复图文，再尝试轮换；期间的新编辑会令匹配失败，不被清除。
    const before = draftSnapshot();
    const probe = {...state, assets:state.assets.map(a=>({...a}))};
    if (rotatePrimary(probe, msg, newId) !== "cleared") return;
    try {repository.save(before); await repository.backup(before);} catch {
      toast("无法保全上次图文，当前稿未清空"); return;
    }
    if (rotatePrimary(state, msg, newId) !== "cleared") return;
    latestMessage = null;
    outbox.disconnect(); if (sessionReady) outbox.connect();
    sendDraft(); update(); toast("已开始下一段，上次图文可召回");
    void refreshSubmit();
  }
  function handleReceipt(msg: any): Promise<void> {
    receiptChain = receiptChain.then(() => handleReceiptNow(msg)).catch(() => {toast("上次回执待确认，当前稿保留");});
    return receiptChain;
  }
  async function clearCurrentDraft() {
    if (clearingDraft || finishingEditor || !restored) return;
    saveOpenEditor();
    clearingDraft = true;
    finishSend(activeSend);
    outbox.disconnect();
    root.querySelector<HTMLElement>(".page")!.inert = true;
    ($("clearDraft") as HTMLButtonElement).disabled = true;
    // Serialize with completion receipts; queued file/capture/upload callbacks
    // are invalidated so an old async result cannot repopulate the new draft.
    receiptChain = receiptChain.then(async () => {
      for (const controller of uploadControllers.values()) controller.abort();
      pendingCapture = "";($("captureBtn") as HTMLButtonElement).disabled=false;
      const before = structuredClone(draftSnapshot());
      const after: SavedDraft = {...before, text:"", assets:[], epoch:newId(),
        generation:(before.generation || 0)+1, revision:0, last_intent:null, saved_at:Date.now()};
      try {
        window.clearTimeout(persistTimer);
        await repository.replaceWithBackup(before, after);
      } catch {
        toast("清空未完成，原稿仍保留；请重试");return;
      }
      finishSend(activeSend);
      ++editorSequence;editorAbort?.abort();editor?.destroy();editor=null;editorLoading=false;
      state.text="";state.assets=[];state.removed=[];state.uploading=false;state.conflict=null;state.last_intent=null;
      state.epoch=after.epoch;state.generation=after.generation!;state.revision=0;
      closeCanvasText();currentId="";
      ($("canvasTextInput") as HTMLTextAreaElement).value="";
      ($("captionInput") as HTMLTextAreaElement).value="";
      ($("captureBtn") as HTMLButtonElement).disabled=false;
      ($("file") as HTMLInputElement).value="";
      $("editor").classList.remove("show");$("composer").style.display="flex";$("mobileHead").style.display="flex";
      closeSheet();forgetBlobs();
      outbox.disconnect();latestMessage=null;
      clearingDraft=false;sendDraft();if(sessionReady)outbox.connect();
      toast("当前图文已清空，可从「最近」恢复；已贴到电脑的内容不会撤回");
    }).catch(() => {toast("清空未完成，请检查当前稿后重试");}).finally(() => {
      clearingDraft=false;root.querySelector<HTMLElement>(".page")!.inert=false;
      publishCurrent();if(sessionReady)outbox.connect();update();void uploadPending();
    });
    await receiptChain;
  }
  $("clearDraft").onclick=()=>{void clearCurrentDraft();};
  async function reconcileReceipt() {
    try {
      const res = await fetch("/v3/phone/event", {headers:headers(), signal:AbortSignal.timeout(2500)});
      if (res.ok) await handleReceipt(await res.json());
      await refreshSubmit();
    } catch { /* 仅取数据，不重放插入动作。 */ }
  }
  async function prepareForDesktop(requestId: string) {
    const message = currentMessage();
    publishCurrent();
    try {
      await outbox.flush(message.update_id, 4500);
      if (currentMessage().update_id !== message.update_id || state.assets.some(a=>!a.asset_id||a.status!=="ready"))
        throw new Error("DRAFT_CHANGED");
      if (ws?.readyState === 1) ws.send(JSON.stringify({...message,type:"draft.prepared",request_id:requestId}));
    } catch {
      if (ws?.readyState === 1) ws.send(JSON.stringify({...message,type:"draft.prepared",request_id:requestId,error:"PHONE_NOT_CURRENT"}));
    }
  }
  window.setInterval(() => {
    if (!ws || ws.readyState !== 1) return;
    if (Date.now()-lastPong > 18000) {ws.close();return;}
    ws.send(JSON.stringify({type:"ping"}));
  },6000);
  window.addEventListener("offline", () => {
    // A lost network must not keep displaying a live socket until the heartbeat expires.
    state.online = false; sessionReady = false; outbox.disconnect();
    finishSend(activeSend); ws?.close(); update();
  });
  window.addEventListener("online", () => {void recoverConnection();});
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {void recoverConnection(); if(sessionReady) publishCurrent();}
  });
  window.addEventListener("pageshow", () => {if(restored) void recoverConnection();});

  let termTimer = 0;
  $("text").addEventListener("input", (e) => {
    state.text = (e.target as HTMLTextAreaElement).value;
    sendDraft();
    update();
    window.clearTimeout(termTimer);
    termTimer = window.setTimeout(() => {
      void refreshTerms().catch(() => {});
    }, 400);
  });
  async function refreshTerms() {
    if (!state.session || !state.online || !state.text.trim() || syncState !== "synced") return;
    const res = await fetch("/v3/terms?q=" + encodeURIComponent(state.text), { headers: headers() });
    if (!res.ok) return;
    const data = await res.json();
    if (data.hints && data.hints.length) toast(String(data.hints[0].hint));
  }
  $("boardBtn").onclick = () => {
    if (state.assets.length >= 6) {toast("一份图文最多六张图片");return;}
    const a: Asset = { id: "board-" + newId(), kind: "白板", preview: "", w: 1600, h: 1000, status: "queued" };
    state.assets.push(a);
    sendDraft();
    update();
    void openNextQueued();
  };
  $("photoBtn").onclick = () => $("file").click();
  $("captureBtn").onclick = () => {
    if (state.assets.length >= 6) {toast("一份图文最多六张图片");return;}
    if (!state.session || !ws || !state.online || ws.readyState !== 1) {toast("截图需要电脑在线；相册和白板仍可使用");return;}
    if (pendingCapture) return;
    pendingCapture = newId(); const requestId = pendingCapture;
    ($("captureBtn") as HTMLButtonElement).disabled = true;update();
    ws.send(JSON.stringify({ protocol: 3, type: "capture.request", session_id: state.session.session_id, token: state.session.token, scope: "primary", request_id:requestId }));
    window.setTimeout(() => {if(pendingCapture===requestId){pendingCapture="";($("captureBtn") as HTMLButtonElement).disabled=false;update();toast("截图未确认；不会自动重复截图，可重新操作");}}, 10000);
  };
  $("cropApply").onclick = () => {
    if (!editor?.applyCrop()) toast("请拖出至少64×64像素的选区");
  };
  $("cropReset").onclick = () => editor?.resetCrop();

  async function fileToDataUrl(file: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  $("file").addEventListener("change", async (e) => {
    if (clearingDraft) return;
    const fileEpoch = state.epoch;
    const files = Array.from((e.target as HTMLInputElement).files || []);
    for (const f of files) {
      if (state.assets.length >= 6) break;
      const allowed = /^(image\/png|image\/jpeg|image\/jpg|image\/webp)$/i.test(f.type) || /\.(png|jpe?g|webp)$/i.test(f.name);
      if (!allowed) {
        toast("不支持的图片类型");
        continue;
      }
      if (f.size > 20*1024*1024) {toast("图片超过20 MiB，请先缩小");continue;}
      const preview = await fileToDataUrl(f);
      if (clearingDraft || state.epoch !== fileEpoch) return;
      const a: Asset = { id: "p-" + newId(), kind: "图片", preview, source: preview };
      const image = new Image();
      image.src = preview;
      try {
        await image.decode();
        if (clearingDraft || state.epoch !== fileEpoch) return;
      } catch {
        if (clearingDraft || state.epoch !== fileEpoch) return;
        toast("图片无法解码");
        continue;
      }
      if (image.naturalWidth*image.naturalHeight > 48_000_000) {toast("图片像素过大，请先缩小");continue;}
      if (state.assets.length >= 6) break;
      a.w = image.naturalWidth || 1;
      a.h = image.naturalHeight || 1;
      a.status = "queued";
      a.source = preview;
      state.assets.push(a);
    }
    (e.target as HTMLInputElement).value = "";
    sendDraft();
    update();
    void openNextQueued();
  });

  function editorOpen(): boolean {
    return $("editor").classList.contains("show");
  }

  async function openNextQueued() {
    if (editorOpen()) return;
    const next = state.assets.find((a) => a.status === "queued" && !a.pending_png);
    if (!next) return;
    try {
      await openEditor(next.kind === "白板" ? "快速白板" : "图片标注", next);
    } catch { editorFailure(); }
  }

  async function openEditor(title: string, asset: Asset) {
    if (editorOpen() || finishingEditor) return;
    uploadControllers.get(asset.id)?.abort();
    if (!("edit_original" in asset)) {
      asset.edit_original = (asset.status === "ready" || asset.pending_png) ? structuredClone(asset) : null;
    }
    asset.pending_png = undefined; asset.upload_ticket = undefined;
    currentId = asset.id;
    const thisEditor = ++editorSequence;
    editorAbort?.abort(); editorAbort = new AbortController();
    const editorSignal = editorAbort.signal;
    editorLoading = true;
    ($("done") as HTMLButtonElement).disabled = true;
    asset.status = "editing";
    asset.render_revision = (asset.render_revision || 1) + 1;
    sendDraft();
    state.editorKind = title;
    $("composer").style.display = "none";
    $("mobileHead").style.display = "none";
    $("editor").classList.add("show");
    $("editTitle").textContent = title;
    $("editorStatus").hidden=true; $("stage").dataset.ready="0";
    ($("captionInput") as HTMLTextAreaElement).value = asset.edit_caption ?? asset.caption ?? "";
    $("extra").classList.remove("show"); $("cropActions").classList.remove("show");
    $("captionDrawer").hidden = true; $("captionToggle").setAttribute("aria-expanded", "false");
    const host = $("stage") as HTMLDivElement;
    editor?.destroy();
    host.replaceChildren();
    closeCanvasText();
    editor = new SharedEditor(host, asset.w || 1600, asset.h || 1000);
    editor.onTextRequest = point => {
      if (textPoint) {$("canvasTextInput").focus();return;}
      textPoint = point;
      $("canvasTextPanel").hidden = false;
      const input = $("canvasTextInput") as HTMLTextAreaElement;
      input.value = ""; input.focus();
    };
    const view = editor;
    document.querySelectorAll("[data-tool]").forEach(b => b.classList.toggle("selected", (b as HTMLElement).dataset.tool === view.tool));
    document.querySelectorAll("[data-color]").forEach(b => b.classList.toggle("selected", (b as HTMLElement).dataset.color === view.color));
    $("widthBtn").textContent = "中 · 5";
    try {
    if (asset.edit_scene || asset.scene) {
      editor.importScene(asset.edit_scene || asset.scene!);
      if (asset.source || asset.preview) {
        let src = asset.source || asset.preview;
        if (src.startsWith("/v3/assets/") && state.session) {
          const res = await fetch(src, { headers: headers(), signal:editorSignal });
          if (!res.ok) throw new Error("SOURCE_READ_FAILED");
          src = await fileToDataUrl(await res.blob());
          if (thisEditor !== editorSequence) return;
          asset.source = src;
        }
        await editor.rebindSource(src);
      }
    } else if (title === "快速白板") {
      editor.addBlankBoard(asset.w || 1600, asset.h || 1000);
      host.dataset.ready = "1";
    } else if (asset.source || asset.preview) {
      let src = asset.source || asset.preview;
      if (src.startsWith("/v3/assets/") && state.session) {
        const res = await fetch(src, { headers: headers(), signal:editorSignal });
        if (!res.ok) {
          throw new Error("SOURCE_READ_FAILED");
        }
        src = await fileToDataUrl(await res.blob());
        if (thisEditor !== editorSequence) return;
        asset.source = src;
      }
      await editor.loadImage(src, asset.w || 1600, asset.h || 1000);
    }
    } catch (error) {if(thisEditor !== editorSequence || editorSignal.aborted)return; throw error;}
    if (thisEditor !== editorSequence || editor !== view) return;
    editorLoading = false;
    ($("done") as HTMLButtonElement).disabled = false;
    host.dataset.ready="1";
    editorStartScene = editor.exportScene();
    if (asset.edit_text?.value) {
      textPoint = {x:asset.edit_text.x, y:asset.edit_text.y};
      ($("canvasTextInput") as HTMLTextAreaElement).value = asset.edit_text.value;
      $("canvasTextPanel").hidden = false;
      chooseTool("text");
    }
    requestAnimationFrame(() => editor?.resize());
    update();
    if (ws?.readyState === 1) ws.send(JSON.stringify({ protocol: 3, type: "editor.activity", kind: "edit" }));
  }

  document.querySelectorAll("[data-tool]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-tool]").forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
      commitCanvasText(); closeCanvasText();
      chooseTool((btn as HTMLElement).dataset.tool as Tool);
      $("cropActions").classList.toggle("show", (btn as HTMLElement).dataset.tool === "crop");
    });
  });
  document.querySelectorAll("[data-color]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-color]").forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
      if (editor) editor.color = (btn as HTMLElement).dataset.color || "#D45243";
    });
  });
  $("widthBtn").onclick = () => {
    if (!editor) return;
    const vals = [2, 5, 10];
    const i = (vals.indexOf(editor.width) + 1) % 3;
    editor.width = vals[i];
    $("widthBtn").textContent = ["细 · 2", "中 · 5", "粗 · 10"][i];
  };
  $("moreBtn").onclick = () => { $("extra").classList.toggle("show"); requestAnimationFrame(()=>editor?.resize()); };
  $("captionToggle").onclick = () => {
    $("captionDrawer").hidden = !$("captionDrawer").hidden;
    $("captionToggle").setAttribute("aria-expanded", String(!$("captionDrawer").hidden));
    requestAnimationFrame(()=>editor?.resize());
  };
  $("wire").onclick = () => editor?.addWireframe();
  $("undoBtn").onclick = () => editor?.undo();
  $("redoBtn").onclick = () => editor?.redo();

  const uploadControllers = new Map<string, AbortController>();
  let uploadQueueRunning = false;
  let uploadWakeRequested = false;
  let finishingEditor = false;
  async function uploadPending() {
    if (uploadQueueRunning) {uploadWakeRequested = true; return;}
    if (clearingDraft || !sessionReady || !state.session) return;
    uploadQueueRunning = true;
    const attempted = new Set<string>();
    try {
      while (!clearingDraft && sessionReady && state.session) {
        const item = state.assets.find(a=>a.pending_png && a.status!=="editing" && a.status!=="ready" && !attempted.has(`${a.id}:${a.render_revision}`));
        if (!item) break;
        attempted.add(`${item.id}:${item.render_revision}`);
        const version = item.render_revision; const uploadEpoch=state.epoch; const blob = item.pending_png!;
        const controller = new AbortController(); uploadControllers.set(item.id,controller);
        state.uploading = true; item.status = "queued"; update();
        try {
          const meta = await uploadPng(blob,headers(),item.w||1,item.h||1,item.kind==="白板"?"whiteboard":"markup",{
            signal:controller.signal,ticket:item.upload_ticket,
            checkpoint: async ticket => {if(clearingDraft || state.epoch!==uploadEpoch || controller.signal.aborted || !state.assets.includes(item))throw new Error("DRAFT_CHANGED");item.upload_ticket=ticket; repository.save(draftSnapshot()); await repository.flush();},
            progress: (sent,total) => {item.progress=Math.floor(100*sent/Math.max(1,total));update();},
          });
          if (!clearingDraft && state.epoch===uploadEpoch && !controller.signal.aborted && state.assets.includes(item) && item.render_revision===version) {
            item.asset_id=meta.asset_id; item.status="ready"; item.pending_png=undefined; item.upload_ticket=undefined;
            item.progress=100; sendDraft();
          }
        } catch {
          if (!clearingDraft && state.epoch===uploadEpoch && !controller.signal.aborted && state.assets.includes(item) && item.render_revision===version && String(item.status)!=="editing") {
            item.status="failed"; item.asset_id=undefined; sendDraft();
          }
        } finally {uploadControllers.delete(item.id); state.uploading=false; update();}
      }
    } finally {
      uploadQueueRunning=false;
      if (uploadWakeRequested) {uploadWakeRequested=false; void uploadPending();}
    }
  }
  async function finishEditor() {
    commitCanvasText();
    if (!editor || finishingEditor || editorLoading) return;
    if (state.editorKind === "快速白板" && editor.ops === 0) {toast("先画一点内容，再加入本次图文");return;}
    finishingEditor = true;
    window.clearTimeout(persistTimer);
    ($("done") as HTMLButtonElement).disabled = true;
    ($("back") as HTMLButtonElement).disabled = true;
    const item = state.assets.find(a=>a.id===currentId);
    let committed = false;
    try {
      if (!item) return;
      const blob = await editor.exportBlob();
      const sceneData = JSON.parse(editor.exportScene());
      if(sceneData.source) sceneData.source.url=item.source||"";
      const preview = await fileToDataUrl(blob);
      const bmp = await createImageBitmap(blob);
      const next: Asset = {...item, caption: ($("captionInput") as HTMLTextAreaElement).value.trim(),
        scene:JSON.stringify(sceneData), preview, w:bmp.width, h:bmp.height, pending_png:blob,
        asset_id:undefined, upload_ticket:undefined, progress:0, status:"queued"};
      bmp.close();
      delete next.edit_original; delete next.edit_scene; delete next.edit_caption; delete next.edit_text;
      const index=state.assets.indexOf(item);
      if(index<0) return;
      // Persist a separate committed candidate before publishing ready/uploadable state.
      // A failed transaction retains both the live canvas and prior saved rendition.
      const snapshot=draftSnapshot(); snapshot.assets[index]={...next};
      try {repository.save(snapshot);await repository.flush();}
      catch {repository.save(draftSnapshot());toast("保存失败，画布和原图都保留；请重试保存或取消");return;}
      state.assets[index]=next; committed=true;
      sendDraft();
      $("editor").classList.remove("show");$("composer").style.display="flex";$("mobileHead").style.display="flex";
      editor.destroy();editor=null;editorLoading=false;
      update();
    } finally {
      finishingEditor=false;($("done") as HTMLButtonElement).disabled=editorLoading;($("back") as HTMLButtonElement).disabled=false;
    }
    if(committed){void uploadPending();void openNextQueued();}
  }

  function saveOpenEditor() {
    if (clearingDraft || !editor || !editorOpen() || finishingEditor) return;
    const item = state.assets.find(a => a.id === currentId);
    if (!item) return;
    try {
      const scene = JSON.parse(editor.exportScene());
      if (scene.source) scene.source.url = item.source || "";
      item.edit_scene = JSON.stringify(scene);
      item.edit_caption = ($("captionInput") as HTMLTextAreaElement).value;
      const pendingText = ($("canvasTextInput") as HTMLTextAreaElement).value;
      item.edit_text = textPoint && pendingText ? {...textPoint, value:pendingText} : undefined;
      repository.save(draftSnapshot());
    } catch { $("localSave").textContent = "当前编辑尚未保存，请保留页面"; }
  }
  $("stage").addEventListener("pointerup", () => window.setTimeout(saveOpenEditor, 0));
  $("captionInput").addEventListener("input", saveOpenEditor);
  $("canvasTextInput").addEventListener("input", saveOpenEditor);
  for (const id of ["undoBtn", "redoBtn", "wire", "cropApply", "cropReset"]) {
    $(id).addEventListener("click", () => window.setTimeout(saveOpenEditor, 0));
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && restored && !clearingDraft) {
      window.clearTimeout(persistTimer);
      saveOpenEditor(); repository.save(draftSnapshot());
    }
  });

  function editorFailure() {
    state.uploading = false; editorLoading = false;
    ($("done") as HTMLButtonElement).disabled = false;
    const item = state.assets.find(a => a.id === currentId);
    if (item) {
      try { if (editor) item.edit_scene = editor.exportScene(); } catch {}
      item.status = "failed";
      sendDraft();
    }
    toast("图片尚未准备好，编辑保留；可重试或返回");
    update();
  }

  $("done").onclick = () => {
    void finishEditor().catch(editorFailure);
  };
  function discardEditor() {
    if (finishingEditor) return;
    const index = state.assets.findIndex(a => a.id === currentId);
    if (index >= 0) {
      const original = state.assets[index].edit_original;
      if (original) state.assets[index] = structuredClone(original);
      else state.assets.splice(index, 1);
    }
    ++editorSequence; editorAbort?.abort();
    editor?.destroy(); editor = null; editorLoading = false;
    $("editor").classList.remove("show"); $("composer").style.display="flex"; $("mobileHead").style.display="flex";
    closeSheet();
    sendDraft(); update(); void uploadPending();
    toast("已放弃未保存的编辑，没有加入新图片");
  }
  function cancelEditor() {
    if (finishingEditor || !editorOpen()) return;
    const item=state.assets.find(a=>a.id===currentId);
    const changed = !editorLoading && editor && ((textPoint && ($("canvasTextInput") as HTMLTextAreaElement).value.trim()) || editor.exportScene() !== editorStartScene ||
      ($("captionInput") as HTMLTextAreaElement).value !== (item?.edit_original?.caption || ""));
    if (!changed) {discardEditor(); return;}
    ++sheetRevision;
    $("sheetCard").innerHTML='<h3>放弃未保存的编辑？</h3><p>已保存的图片不会改变。新建但未保存的白板不会加入图文。</p><button id="keepEditing" class="primary">继续编辑</button> <button id="discardEditing">放弃更改</button>';
    openSheet();
    $("keepEditing").onclick=()=>closeSheet();
    $("discardEditing").onclick=discardEditor;
  }
  $("back").onclick = cancelEditor;
  window.addEventListener("beforeunload", e=>{if(editorOpen()){saveOpenEditor();e.preventDefault();e.returnValue="";}});
  const stageResize = new ResizeObserver(()=>{if(editor&&!editorLoading) editor.resize();});
  stageResize.observe($("stage"));

  $("connectBtn").onclick = () => {void recoverConnection(true);};
  $("sendBtn").onclick = () => { void sendBundle(); };
  async function sendBundle() {
    if (activeSend || state.sending) return;
    if (!state.session || !ws || !state.online) {
      toast("未连接，草稿保留");
      return;
    }
    if (!state.text.trim() && !state.assets.length) return;
    let refs: string[];
    try {
      refs = assetRefs();
    } catch {
      toast("还有图片没传完，不会先发残缺文字");
      return;
    }
    const message = currentMessage();
    const boundRevision = state.revision;
    const boundEpoch = state.epoch;
    const socket = ws;
    const session = {...state.session};
    const operation = {intentId: "", timer: 0};
    activeSend = operation;
    let dispatched = false;
    const isCurrent = () => activeSend === operation && ws === socket && socket.readyState === WebSocket.OPEN;
    state.sending = true; update();
    try {
    publishCurrent();
    try {await outbox.flush(message.update_id);} catch {
      if (isCurrent()) toast("当前图文仍在同步，没有插入旧版本");return;
    }
    if (!isCurrent()) return;
    const nonceRes = await fetch("/v3/nonce", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(session), signal:AbortSignal.timeout(6000) });
    if (!isCurrent()) return;
    if (!nonceRes.ok) {
      toast("请在电脑确认插入权限。拒绝后仍可同步文字。练习插入用电脑 Alt+I。");
      return;
    }
    const nonce = await nonceRes.json();
    if (!isCurrent()) return;
    if (state.revision !== boundRevision || state.epoch !== boundEpoch) {
      toast("内容刚有更新，请再次点插入");
      return;
    }
    const signature=JSON.stringify([state.draft_id,state.epoch,state.generation,state.revision]);
    const intentId=state.last_intent?.signature===signature ? state.last_intent.id : newId();
    operation.intentId = intentId;
    state.last_intent={signature,id:intentId};
    repository.save(draftSnapshot());
    try {await repository.flush();} catch {if(isCurrent())toast("无法保存本次操作，未执行插入");return;}
    if (!isCurrent()) return;
    if (state.revision!==boundRevision || state.epoch!==boundEpoch) {
      toast("图文或连接有变化，请再次确认");return;
    }
    socket.send(
      JSON.stringify({
        ...message,
        protocol: 3,
        type: "insert.intent",
        session_id: session.session_id,
        token: session.token,
        nonce: nonce.nonce,
        intent_id: intentId,
        trigger: "phone",
        text: state.text,
        revision: state.revision,
        asset_refs: refs,
        draft_id: state.draft_id,
        epoch: state.epoch,
        captions: state.assets.map((a) => a.caption || ""),
        asset_status: state.assets.map((a) => a.status || "ready"),
        asset_documents: documents(),
      })
    );
    dispatched = true;
    operation.timer = window.setTimeout(()=>{
      if (finishSend(operation)) {
        update();toast("上次结果待确认，图文保留；再次点同一稿不会重复投递，可从最近内容召回");
      }
    },65000);
    } catch {
      if (isCurrent()) toast("连接中断，图文已保留，没有自动重试");
    } finally {
      if (!dispatched && finishSend(operation)) update();
    }
  };

  const sendErrors: Record<string,string> = {
    PHONE_SEND_DISABLED:"请先在电脑常用设置开启「允许手机确认后发送」",
    TARGET_CHANGED:"电脑输入框已改变。请点回刚才的输入框再确认，没有发送。",
    SEND_EXPIRED:"这份发送操作已过期，请在电脑处理；不会自动重贴。",
    SEND_DRAFT_CHANGED:"你已开始编辑新内容，上一份不会再由旧按钮发送。",
    SEND_ALREADY_USED:"这次发送已处理，不会重复发送。",
    SEND_SETTINGS_CHANGED:"发送快捷键已改变，请重新确认。",
    SEND_PERMISSION_DENIED:"插入权限已取消，没有发送。",
    SEND_RESULT_UNKNOWN:"系统发送结果未知，请查看电脑；不会自动重试。",
    BUSY:"正在处理另一项操作，没有排队发送。",
  };
  async function refreshSubmit() {
    if(!state.online || !state.session) return;
    const epoch=state.epoch,rev=state.revision;
    try {
      const response=await fetch("/v3/send/status",{headers:headers(),signal:AbortSignal.timeout(3000)});
      if(!response.ok) return;
      const info=await response.json();
      if(state.epoch!==epoch || state.revision!==rev || state.text || state.assets.length) return;
      submitAvailable=info.available===true;
      $("submitPanel").hidden=!submitAvailable;
    } catch { /* 查询失败不触发命令。 */ }
  }
  window.setInterval(()=>{if(sessionReady && !submitBusy && !editorOpen() && !state.text && !state.assets.length)void refreshSubmit();},6000);
  $("submitMessage").onclick = () => {void confirmSubmit();};
  async function confirmSubmit() {
    if(!submitAvailable || submitBusy || !state.session || !state.online) return;
    submitBusy=true; const originalSession=state.session; const epoch=state.epoch,rev=state.revision;
    try {
      const response=await fetch("/v3/send/prepare",{method:"POST",headers:headers(),signal:AbortSignal.timeout(4000)});
      const info=await response.json();
      if(!response.ok || info.error_code){toast(sendErrors[info.error_code]||"当前不能发送，图文仍在电脑");return;}
      if(state.epoch!==epoch || state.revision!==rev || state.session!==originalSession || !state.online) return;
      ++sheetRevision;
      $("sheetCard").innerHTML='<h3>发送电脑上的上一份图文？</h3><p>请先看电脑，确认图片、文字与目标都正确。这一步只发送，不重复粘贴。</p><p id="submitShortcut"></p><button id="confirmSend" class="primary">确认发送</button> <button id="cancelSend">取消</button>';
      $("submitShortcut").textContent=`将执行 ${info.shortcut}，不会自动重试。`;
      openSheet();
      $("cancelSend").onclick=()=>closeSheet();
      $("confirmSend").onclick=()=>{void (async()=>{
        const btn=$("confirmSend") as HTMLButtonElement; if(btn.disabled)return;btn.disabled=true;
        if(state.epoch!==epoch || state.revision!==rev || state.session!==originalSession || !state.online){toast("内容或连接已变化，没有发送");closeSheet();return;}
        try {
          const result=await fetch("/v3/send/commit",{method:"POST",headers:{...headers(),"Content-Type":"application/json"},
            body:JSON.stringify({ticket:info.ticket,delivery_id:info.delivery_id,confirmed:true}),signal:AbortSignal.timeout(6000)});
          const status=await result.json();
          toast(status.result==="KEYS_SENT"?"发送按键已执行，请查看电脑结果":sendErrors[status.error_code]||"发送未确认，请查看电脑，不会自动重试");
        }catch{toast("发送回执未收到，请看电脑结果；不会自动重试");}
        finally {submitAvailable=false;$("submitPanel").hidden=true;closeSheet();}
      })();};
    }catch{toast("电脑未连接，没有发送");}finally{submitBusy=false;}
  }

  function pairCodeFromUrl(): string {
    try {
      return new URL(location.href).searchParams.get("pair") || "";
    } catch {
      return "";
    }
  }

  const DEVICE_KEY = "dt.v3.device";

  function storeDevice(deviceId: string, secret: string) {
    try {
      const value = JSON.stringify({ device_id: deviceId, device_secret: secret });
      localStorage.setItem(DEVICE_KEY, value);
      return localStorage.getItem(DEVICE_KEY) === value;
    } catch {
      return false;
    }
  }

  async function pullRememberedSecret() {
    const session = state.session;
    if (!session || !sessionReady) return;
    window.clearTimeout(secretRetryTimer);
    const authHeaders = {"X-DT-Session": session.session_id, "X-DT-Token": session.token};
    try {
      const res = await fetch("/v3/device/secret", { headers: authHeaders, signal: AbortSignal.timeout(6000) });
      if (!res.ok) throw new Error("secret unavailable");
      const body = await res.json();
      if (state.session !== session || !body.device_secret || body.device_id !== session.device_id) return;
      if (!storeDevice(String(body.device_id), String(body.device_secret))) {
        toast("浏览器未能保存配对，请使用允许存储的普通浏览器；当前仍可使用");
        return;
      }
      const ack = await fetch("/v3/device/secret/ack", {
        method: "POST", headers: {...authHeaders, "Content-Type": "application/json"},
        body: JSON.stringify({device_secret: body.device_secret}), signal: AbortSignal.timeout(6000),
      });
      if (!ack.ok) throw new Error("secret acknowledgement unavailable");
      if (state.session === session) toast("已记住，下次自动续接");
    } catch {
      if (state.session === session && sessionReady)
        secretRetryTimer = window.setTimeout(() => {void pullRememberedSecret();}, 3000);
    }
  }

  async function recoverConnection(showMissing = false) {
    if (state.session) {connect(); return;}
    if (!await resumeRemembered() && showMissing) showPair();
  }

  function resumeRemembered(): Promise<boolean> {
    if (state.session) return Promise.resolve(true);
    if (resumeFlight) return resumeFlight;
    resumeFlight = attemptRemembered().finally(() => {resumeFlight = null;});
    return resumeFlight;
  }

  async function attemptRemembered(): Promise<boolean> {
    let stored: { device_id?: string; device_secret?: string } | null = null;
    try {
      stored = JSON.parse(localStorage.getItem(DEVICE_KEY) || "null");
    } catch {
      stored = null;
    }
    if (!stored?.device_id || !stored.device_secret) {resumePending = false; return false;}
    resumePending = true;
    update();
    window.clearTimeout(reconnectTimer);
    try {
      const res = await fetch("/v3/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: stored.device_id, device_secret: stored.device_secret }),
        signal: AbortSignal.timeout(6000),
      });
      if (!res.ok) {
        const body = await res.json();
        if (res.status === 400 && ["device expired", "device mismatch"].includes(body.error)) {
          localStorage.removeItem(DEVICE_KEY);
          resumePending = false;
          update();
          toast("此配对已过期或被撤销，请重新扫码；草稿仍在");
          return false;
        }
        throw new Error("resume unavailable");
      }
      state.session = await res.json();
      resumePending = false;
      try {sessionStorage.setItem("dt.v3.session", JSON.stringify(state.session));} catch { /* local credential still persists */ }
      const clean = new URL(location.href); clean.searchParams.delete("pair");
      history.replaceState(history.state, "", clean.pathname + clean.search + clean.hash);
      if (document.getElementById("pairStatus")) closeSheet();
      connect(); update();
      return true;
    } catch {
      // 离线/超时/服务暂未启动不代表配对失效；保留凭据并自动再试。
      reconnectTimer = window.setTimeout(() => {void recoverConnection(true);}, 3000);
      update();
      return true;
    }
  }

  async function submitPair(code: string, panelVersion: number): Promise<boolean> {
    if (await resumeRemembered()) return true;
    const res = await fetch("/v3/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
      signal: AbortSignal.timeout(6000),
    });
    if (!res.ok) return false;
    state.session = await res.json();
    sessionStorage.setItem("dt.v3.session", JSON.stringify(state.session));
    const clean = new URL(location.href); clean.searchParams.delete("pair");
    history.replaceState(history.state, "", clean.pathname + clean.search + clean.hash);
    if (sheetRevision === panelVersion) closeSheet();
    connect();
    update();
    return true;
  }

  function showPair() {
    const version = ++sheetRevision;
    const preset = pairCodeFromUrl();
    $("sheetCard").innerHTML = `<h2>连接电脑</h2><p id="pairStatus" role="status">扫描电脑连接页当前显示的二维码，就能连接。草稿会保留在手机。</p><details><summary>无法扫码？使用备用短码</summary><input id="pairCode" aria-label="备用配对短码" inputmode="numeric" placeholder="电脑显示的 4 位短码" /><button class="primary" id="pairGo">使用短码连接</button></details><button id="writeOffline">继续写草稿</button>`;
    openSheet();
    $("writeOffline").onclick = () => closeSheet();
    const pair = async (code:string, scanned:boolean) => {
      const button = $("pairGo") as HTMLButtonElement; button.disabled = true;
      $("pairStatus").textContent = "正在连接电脑…";
      try {
        const paired = await submitPair(code, version);
        if (sheetRevision !== version) return;
        if (!paired) $("pairStatus").textContent = scanned ?
          "这个二维码已过期或已使用。电脑连接页会自动刷新，请重新扫描当前二维码；手机草稿仍在。" :
          "短码无效或已过期，请核对电脑当前显示的短码。";
      } catch { if (sheetRevision === version) $("pairStatus").textContent = "暂时连不上电脑。请确认同一网络；可以继续离线写草稿。"; }
      finally {button.disabled = false;}
    };
    $("pairGo").onclick = () => {void pair(($("pairCode") as HTMLInputElement).value, false);};
    if (preset) void pair(preset, true);
  }

  function showRestoreProposal(message: any) {
    const version = ++sheetRevision;
    const card=$("sheetCard");card.replaceChildren();
    const title=document.createElement("h2");title.textContent="恢复为手机新稿？";
    const desc=document.createElement("p");desc.textContent="当前图文会先在手机保留副本，恢复后仍由手机编辑。不会自动插入。";
    const text=document.createElement("p");text.textContent=String(message.text||"").slice(0,160);
    const accept=document.createElement("button");accept.className="primary";accept.textContent="保留当前，恢复图文";
    const cancel=document.createElement("button");cancel.textContent="取消";cancel.onclick=()=>closeSheet();
    card.append(title,desc,text,accept,cancel);openSheet();
    accept.onclick=async()=>{
      if (accept.disabled) return;
      accept.disabled = true;
      saveOpenEditor();const before=draftSnapshot();
      try{repository.save(before);await repository.backup(before);}catch{accept.disabled=false;toast("手机存储失败，未替换当前稿");return;}
      if (sheetRevision !== version) return;
      for(const controller of uploadControllers.values())controller.abort();
      state.text=String(message.text||"");
      state.assets=message.local_snapshot ? structuredClone(message.local_snapshot.assets||[]) :
        (message.assets||[]).map((a:any)=>({...a,id:a.id||newId(),kind:"图片",preview:`/v3/assets/${a.asset_id}`,status:"ready"}));
      state.epoch=newId();state.generation++;state.revision=0;state.conflict=null;
      editor?.destroy();editor=null;$("editor").classList.remove("show");$("composer").style.display="flex";$("mobileHead").style.display="flex";
      closeSheet();latestMessage=null;outbox.disconnect();if(sessionReady)outbox.connect();sendDraft();update();void uploadPending();
    };
  }

  $("historyBtn").onclick = async () => {
    const version=++sheetRevision;
    const card=$("sheetCard");card.innerHTML="<h2>最近图文</h2><p>恢复为新稿不自动插入；上次完整图文仍可在电脑召回。</p>";
    openSheet();
    try {
      const local=await repository.load("before-replace");
      if(sheetRevision!==version)return;
      if(local){
        const preview=document.createElement("p");preview.textContent=`手机上次 · ${local.assets.length} 图 · ${local.text.slice(0,90)}`;
        const restore=document.createElement("button");restore.textContent="恢复手机上次图文（可离线）";
        restore.onclick=()=>showRestoreProposal({text:local.text,local_snapshot:local});card.append(preview,restore);
      }
    }catch{toast("无法读取手机恢复记录，当前稿不受影响");}
    if(sheetRevision!==version)return;
    if (!state.session || !state.online) {
      const note=document.createElement("p");note.textContent="电脑离线；本机记录仍可恢复。";card.append(note);return;
    }
    try{
      const res=await fetch("/v3/history",{headers:headers(),signal:AbortSignal.timeout(6000)});
      if(!res.ok)throw new Error("offline");const data=await res.json();if(sheetRevision!==version)return;
      for(const item of data.items||[]){const row=document.createElement("div");row.textContent=`电脑记录 · ${item.asset_count} 图 · ${item.text_chars} 字`;card.append(row);}
      const recall=document.createElement("button");recall.className="primary";recall.id="recallLast";recall.textContent="电脑召回上次（保留手机当前稿）";
      recall.onclick=()=>{if(ws?.readyState===1)ws.send(JSON.stringify({protocol:3,type:"recall.last"}));closeSheet();};card.append(recall);
    }catch{if(sheetRevision!==version)return;const note=document.createElement("p");note.textContent="电脑记录暂未读到，可稍后再试。";card.append(note);}
  };
  $("settingsBtn").onclick = () => {
    const version = ++sheetRevision;
    $("sheetCard").innerHTML = `<h2>连接与设置</h2>
      <button id="motionToggle" aria-pressed="${inputMotion.enabled}"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 10c3-5 5 5 8 0s5 5 8 0M4 16c3-5 5 5 8 0s5 5 8 0"/></svg> 界面动效 · ${inputMotion.enabled?'开':'关'}</button>
      <p id="settingsStatus" role="status">正在读取电脑状态…</p><p id="settingsVersion"></p><p id="settingsGrant"></p>
      <h3>日常怎么用</h3><ol class="quick-start"><li>在手机写字、说话或加图，内容自动同步。</li><li>在电脑点一下要输入的位置，再点手机「插入电脑」或电脑「插入并复制」。</li><li>插入完成后写下一段；上一份可在「最近」找回。</li></ol>
      <details><summary>连接不上或插入不了</summary><p>确认手机和电脑在同一网络，并扫描正在运行的这一版二维码。电脑连接页可开启本机的插入、截图权限。</p><p>自动插入失败时，在电脑选择输入框再粘贴；图片结果不确定时，从电脑浮窗「恢复」查看已完成的部分。</p><p>断线仍可写草稿，重连后继续同步。连接和重连不会自动插入。</p></details>
      <details><summary>AI 辅助与确认发送</summary><p>手机听写使用输入法，不需要配置 AI 密钥。AI 改写仅在电脑主动调用。</p><p>需要手机确认发送时，在电脑常用设置开启「允许手机确认后发送」。插入完成后，另行核对电脑内容并确认发送。</p></details>`;
    openSheet();
    $('motionToggle').onclick=()=>{
      inputMotion.setEnabled(!inputMotion.enabled);
      $('motionToggle').setAttribute('aria-pressed',String(inputMotion.enabled));
      $('motionToggle').lastChild!.textContent=` 界面动效 · ${inputMotion.enabled?'开':'关'}`;
    };
    void fetch("/v3/status", {headers:headers(),signal:AbortSignal.timeout(6000)}).then(async res => {
      if (!res.ok) throw new Error("offline");
      const st = await res.json();
      if (sheetRevision !== version) return;
      $("settingsStatus").textContent = state.online ? "已连接电脑 · " + location.host : "电脑可访问，正在恢复连接";
      const source = st.build?.source_sha;
      $("settingsVersion").textContent = source ? `电脑版本：${source === "development" ? "开发预览" : "V3 体验版 · " + source.slice(0,8)}（可与电脑连接页核对）` : "电脑未提供版本信息";
      $("settingsGrant").textContent = st.permissions ?
        `手机插入：${st.permissions.insert ? "已允许" : "未开启"} · 截电脑：${st.permissions.capture ? "已允许" : "未开启"}。可在电脑连接页调整。` : "尚未配对，可先继续写草稿。";
    }).catch(() => {
      if (sheetRevision === version) $("settingsStatus").textContent = "电脑未连接，草稿仍保留";
    });
  };

  async function resolveConflict(useLocal: boolean) {
    const remote = state.conflict;
    if (!remote) return;
    if (remote.authority === "phone") {
      if (!useLocal) return;
      try {repository.save(draftSnapshot());await repository.backup(draftSnapshot());}
      catch {toast("当前稿尚未保全，暂不切换");return;}
      // 只有用户明确采用手机这份时才建立新一代稿，旧稿先由服务端保全。
      state.epoch=newId();state.generation=Math.max(state.generation,Number(remote.generation)||0)+1;state.revision=0;
      latestMessage = buildPrimaryUpdate(state,newId());
      latestMessage.takeover = {draft_id:remote.draft_id,epoch:remote.epoch,revision:remote.revision,owner_device_id:remote.owner_device_id};
      state.conflict=null;outbox.disconnect();if(sessionReady)outbox.connect();
      publishCurrent();update();return;
    }
    // 明确操作前保全本机稿；不把跨epoch旧稿自动伪装成服务器新稿。
    try { repository.save(draftSnapshot()); await repository.backup(draftSnapshot()); } catch {
      toast("无法保存恢复副本，暂不替换草稿"); return;
    }
    state.draft_id = remote.draft_id;
    state.epoch = remote.epoch;
    state.revision = remote.revision || 0;
    state.conflict = null;
    if (useLocal) sendDraft();
    else {
      state.text = remote.text || "";
      state.assets = remote.assets || (remote.asset_refs || []).map((id: string) => ({id, asset_id:id, kind:"图片", preview:`/v3/assets/${id}`, status:"ready"}));
    }
    update();
  }
  $("useLocal").onclick = () => { void resolveConflict(true); };
  $("useServer").onclick = () => { void resolveConflict(false); };
  $("sheet").onclick = (e) => {
    if (e.target === $("sheet")) closeSheet();
  };
  document.addEventListener("keydown", (e) => {
    if (!$("sheet").classList.contains("show")) return;
    if (e.key === "Escape") {e.preventDefault(); closeSheet(); return;}
    if (e.key !== "Tab") return;
    const controls = Array.from($("sheetDialog").querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),summary,[tabindex="0"]'))
      .filter(el => el.getClientRects().length > 0);
    const first = controls[0], last = controls[controls.length-1];
    if (e.shiftKey && document.activeElement === first) {e.preventDefault(); last?.focus();}
    else if (!e.shiftKey && document.activeElement === last) {e.preventDefault(); first?.focus();}
  });

  try {
    state.session = JSON.parse(sessionStorage.getItem("dt.v3.session") || "null");
  } catch {
    state.session = null;
  }
  ($("text") as HTMLTextAreaElement).disabled = true;
  void restoreDraft().then(async () => {
    ($("text") as HTMLTextAreaElement).disabled = false;
    update(); persistDraft();
    await recoverConnection(true);
  }).catch(() => {
    restored = true;
    ($("text") as HTMLTextAreaElement).disabled = false;
    update();toast("电脑未连接，草稿仍在；可先继续写");
    if (!state.text && !state.assets.length) showPair();
  });
}
