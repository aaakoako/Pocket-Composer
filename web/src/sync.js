// 同一源稿的回执才允许轮换；传输确认不等于目标应用已收到。
export function receiptMatches(state, archived) {
  if (!archived || archived.draft_id !== state.draft_id || archived.epoch !== state.epoch || archived.revision !== state.revision) return false;
  if (String(archived.source_text ?? archived.text ?? "") !== state.text) return false;
  const items = state.assets || [];
  if (items.some(a => !a.asset_id || (a.status && a.status !== "ready"))) return false;
  const refs = archived.asset_refs || [];
  if (refs.length !== items.length || items.some((a, i) => a.asset_id !== refs[i])) return false;
  if (Array.isArray(archived.assets)) {
    if (archived.assets.length !== items.length) return false;
    return items.every((a, i) => {
      const b = archived.assets[i];
      return (a.id || a.asset_id) === b.id && a.asset_id === b.asset_id &&
        (a.render_revision || 1) === (b.render_revision || 1) &&
        (a.caption || "") === (b.caption || "") && (b.status || "ready") === "ready";
    });
  }
  // 旧回执不能确认带版本/图注的新版素材，宁可保留，也不误清。
  return items.every(a => !(a.caption || "") && (a.render_revision || 1) === 1);
}

export function applyRotated(state, msg) {
  if (msg.rotated === false || !receiptMatches(state, msg.archived)) return "kept";
  if (!msg.epoch || msg.epoch === state.epoch || msg.draft_id !== state.draft_id) return "kept";
  state.text = "";
  state.assets = [];
  state.epoch = String(msg.epoch);
  state.revision = Number(msg.revision);
  state.conflict = null;
  return "cleared";
}

function contentMatches(state, msg) {
  if (typeof msg.text === "string" && msg.text !== state.text) return false;
  const refs = msg.asset_refs;
  if (Array.isArray(refs)) {
    if (refs.length !== state.assets.length) return false;
    if (state.assets.some((a, i) => a.asset_id !== refs[i] || (a.status && a.status !== "ready"))) return false;
  }
  return true;
}

export function applyReady(state, msg) {
  const same = state.draft_id === msg.draft_id && state.epoch === msg.epoch;
  const hasLocal = !!(state.text || state.assets.length);
  const hasRemote = !!(msg.text || (msg.asset_refs || []).length);
  if ((!same && hasLocal && (state.draft_id || hasRemote)) || (same && !contentMatches(state, msg) && hasLocal)) {
    state.conflict = { ...msg };
    return "conflict";
  }
  if (!same || !hasLocal) {
    state.draft_id = String(msg.draft_id || state.draft_id);
    state.epoch = String(msg.epoch || state.epoch);
    if (!hasLocal && typeof msg.text === "string") state.text = msg.text;
    if (!hasLocal && Array.isArray(msg.assets)) state.assets = msg.assets;
    else if (!hasLocal && hasRemote && (msg.asset_refs || []).length) {
      state.assets = msg.asset_refs.map(id => ({id, asset_id:id, kind:"图片", preview:`/v3/assets/${id}`, status:"ready"}));
    }
    if (Number.isInteger(msg.revision)) state.revision = msg.revision;
    state.conflict = null;
    return "adopt";
  }
  if (Number.isInteger(msg.revision)) state.revision = Math.max(state.revision, msg.revision);
  state.conflict = null;
  return "same";
}

/** 每次本地修改先形成版本化消息，即使离线/图片未完成也要使旧回执过期。 */
export function buildDraftUpdate(state) {
  state.revision = (Number(state.revision) || 0) + 1;
  return {
    protocol: 3, type: "draft.update", text: state.text,
    revision: state.revision, draft_id: state.draft_id, epoch: state.epoch,
    asset_refs: (state.assets || []).filter(a => a.asset_id).map(a => a.asset_id),
    asset_documents: (state.assets || []).map(a => ({
      id: a.id || a.asset_id, asset_id: a.asset_id || "", status: a.status || "ready",
      render_revision: a.render_revision || 1, caption: a.caption || ""
    }))
  };
}


/** 手机生成身份与版本。重连不采纳电脑较旧的正文或 epoch。 */
export function buildPrimaryUpdate(state, updateId) {
  return {...buildDraftUpdate(state), authority: "phone", generation: state.generation || 0,
    update_id: updateId};
}

/** 同一份手机源稿才可开始下一段；新一段由手机创建，不由旧服务器回执命名。 */
export function rotatePrimary(state, msg, makeId) {
  if (!msg.phone_primary || !msg.rotated || msg.generation !== (state.generation || 0) || !receiptMatches(state, msg.archived)) return "kept";
  state.text = ""; state.assets = []; state.epoch = makeId();
  state.generation = (state.generation || 0) + 1; state.revision = 0;
  state.conflict = null;
  return "cleared";
}

/** 一个在途快照 + 一个已落盘待发快照 + 一个合并后的最新快照。只重试数据，从不重试插入命令。
 * 等待电脑 ACK 时就把下一份快照写入手机存储（流水线），ACK 一到即可发出，
 * 慢存储不会让每一跳都串行等待“写盘 + 往返”。
 * 发送间隔至少 minIntervalMs：本地 ACK 只要几毫秒，连续快速输入若每份都发会超过电脑端的
 * 连接限速（被拒后整条同步停住）。空闲后的第一份立即发出；间隔内新到的编辑只替换待发快照，
 * 最后一份总会在间隔结束后发出。电脑仍回 rate limited 时退回待发并稍后重发，不当作冲突。 */
export class DraftOutbox {
  constructor({send, persist, onState = () => {}, timer = (fn, ms) => setTimeout(fn, ms),
               cancel = id => clearTimeout(id), retryMs = 1600, debounceMs = 0,
               minIntervalMs = 0, backoffMs = 1000, now = () => Date.now()}) {
    Object.assign(this, {send, persist, onState, timer, cancel, retryMs, debounceMs, minIntervalMs, backoffMs, now});
    this.latest = null; this.flight = null; this.ready = null; this.acked = null; this.online = false;
    this.timeout = null; this.preparing = false; this.closed = false; this.waiters = [];
    this.failure = null; this.connectionVersion = 0;
    this.lastSentAt = -Infinity; this.holdUntil = -Infinity; this.wakeTimer = null;
  }
  offer(message) {
    this.latest = structuredClone(message); this.failure = null;
    this.onState(this.online ? "syncing" : "offline");
    this.settle(); void this.pump();
  }
  connect() {
    this.connectionVersion++;
    this.online = true; this.flight = null; this.ready = null; this.acked = null; this.failure = null;
    this.holdUntil = -Infinity;
    this.cancel(this.timeout); this.stopWake(); void this.pump();
  }
  disconnect() {
    this.connectionVersion++;
    this.online = false; this.flight = null; this.ready = null; this.cancel(this.timeout); this.stopWake();
    this.onState("offline");
  }
  stopWake() { if (this.wakeTimer !== null) { this.cancel(this.wakeTimer); this.wakeTimer = null; } }
  /** 距离下一次允许发送还有多少毫秒（发送间隔与限速退避取较晚者）。 */
  sendDelay() {
    return Math.max(this.lastSentAt + this.minIntervalMs, this.holdUntil) - this.now();
  }
  async pump() {
    if (this.closed || !this.online || !this.latest || this.failure) return;
    if (!this.flight && this.ready) {
      const wait = this.sendDelay();
      if (wait > 0) {
        if (this.wakeTimer === null) this.wakeTimer = this.timer(() => { this.wakeTimer = null; void this.pump(); }, wait);
      } else {
        // 已在本连接落盘的快照：上一份 ACK 且间隔已到后立即发出。
        this.flight = this.ready; this.ready = null; this.transmit();
      }
    }
    if (this.preparing) return;
    const id = this.latest.update_id;
    if (!this.flight && !this.ready && this.acked === id) { this.onState("synced"); this.settle(); return; }
    if (this.acked === id || this.flight?.update_id === id || this.ready?.update_id === id) return;
    this.preparing = true;
    // 固定这次即将发出的快照。等待本地写盘/合并窗口时产生的新编辑只替换 latest，
    // 不得使本次快照无限延期。内存最多保留一个在途、一个已落盘待发和一个最新快照；
    // 待发快照未发出前又准备好更新的一份，就由更新的一份替换（电脑只需要最新版）。
    const preparingMessage = structuredClone(this.latest);
    const connection = this.connectionVersion;
    let prepared = false;
    try {
      if (this.debounceMs) await new Promise(resolve => this.timer(resolve, this.debounceMs));
      if (!this.online || this.closed || connection !== this.connectionVersion) return;
      await this.persist();
      if (!this.online || this.closed || !this.latest || connection !== this.connectionVersion || this.failure) return;
      prepared = true;
      this.ready = preparingMessage;
    } catch { this.onState("save_failed"); }
    finally {
      this.preparing = false;
      // 断开后重连时，旧连接等待写盘的快照不可跨会话发出；准备好则尝试发出，期间又有新编辑则继续准备下一份。
      if (this.online && !this.closed && (connection !== this.connectionVersion || prepared)) void this.pump();
    }
  }
  transmit() {
    if (!this.flight || !this.online || this.closed) return;
    this.cancel(this.timeout);
    this.lastSentAt = this.now();
    try { this.send(this.flight); this.onState("syncing"); }
    catch { this.onState("offline"); }
    this.timeout = this.timer(() => {
      if (!this.online || this.closed) return;
      this.onState("retrying");
      this.transmit();
    }, this.retryMs);
  }
  acknowledge(ack) {
    const f = this.flight;
    if (!f || ack.update_id !== f.update_id || ack.draft_id !== f.draft_id ||
        ack.epoch !== f.epoch || ack.revision !== f.revision || ack.generation !== f.generation) return false;
    if (!ack.durable || ack.error || ack.parked) return false;
    this.cancel(this.timeout); this.acked = f.update_id; this.flight = null;
    this.settle(); void this.pump(); return true;
  }
  /** 电脑因连接限速拒收这份快照：不是内容冲突。退回待发（已有更新的待发则用更新的），稍后重发。 */
  defer(ack) {
    const f = this.flight;
    if (!f || !ack?.update_id || ack.update_id !== f.update_id) return false;
    this.cancel(this.timeout); this.flight = null;
    if (!this.ready) this.ready = f;
    this.holdUntil = this.now() + this.backoffMs;
    this.onState("retrying"); void this.pump(); return true;
  }
  reject(ack) {
    // 只有对应在途快照的拒绝才停止同步；与草稿无关的错误（心跳、其他请求）不影响 outbox。
    if (!this.flight || !ack?.update_id || ack.update_id !== this.flight.update_id) return;
    this.cancel(this.timeout); this.flight = null; this.ready = null; this.failure = ack.error || "SYNC_REJECTED";
    this.onState("conflict"); this.settle();
  }
  settle() {
    for (const w of [...this.waiters]) {
      if (this.failure || (this.latest && w.id !== this.latest.update_id)) {
        w.done(new Error(this.failure || "DRAFT_CHANGED"));
      } else if (w.id === this.acked) w.done(null);
    }
  }
  flush(id = this.latest?.update_id, ms = 6000) {
    if (!id) return Promise.reject(new Error("NO_DRAFT"));
    if (this.online && id === this.acked) return Promise.resolve();
    if (this.latest?.update_id !== id) return Promise.reject(new Error("DRAFT_CHANGED"));
    return new Promise((resolve, reject) => {
      const entry = {id, done: error => {
        this.cancel(timeout); this.waiters = this.waiters.filter(w => w !== entry);
        error ? reject(error) : resolve();
      }};
      const timeout = this.timer(() => entry.done(new Error("SYNC_TIMEOUT")), ms);
      this.waiters.push(entry); this.settle(); void this.pump();
    });
  }
  close() {
    this.closed = true; this.disconnect(); this.stopWake();
    for (const w of [...this.waiters]) w.done(new Error("CLOSED"));
  }
}
