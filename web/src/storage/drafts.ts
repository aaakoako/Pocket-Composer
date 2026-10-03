/** 手机工作稿：图片像素与场景存 IndexedDB，不把大图塞进 sessionStorage。
 * 每次写入同一事务；失败保留内存及上次成功记录，并由界面明确提示。
 * 凭据不在本模块中保存。浏览器清理/私密模式仍可能移除数据，不承诺永久保存。
 *
 * 同一手机可能开着多个标签页：claim() 把本页登记为当前写入者（同一事务读出主稿）。
 * 之后若别的页面接管，本页对主稿的写入在事务内被拒绝，被拒的快照另存为 side-* 记录。
 *
 * 大字段（图片 data URL、画布场景等）另存为只写一次的 payload-* 记录，稿件记录只保存引用；
 * 引用与内容写在同一事务里，并在事务内确认 payload 仍在，所以连续说话时每次写盘只写文字级别的
 * 数据，而不是每个字都把几 MB 图片重写一遍。未被任何稿件引用的 payload 在接管/空闲时清理。
 */
export type SavedDraft = {
  schema: 1; text: string; revision: number; draft_id: string; epoch: string;
  assets: Record<string, unknown>[];
  saved_at: number; generation?: number; last_intent?: {signature:string;id:string}|null;
};
export type SaveState = "saving" | "saved" | "unavailable" | "superseded";
export type SideDraft = {key: string; draft: SavedDraft};
const DATABASE = "doubao-typeless-v3-drafts";
const STORE = "drafts";
const WRITER = "writer";
const SIDE_PREFIX = "side-";
const PAYLOAD_PREFIX = "payload-";
/** 超过此长度的字符串字段另存为 payload；短字段（图注、状态、编号）仍内联。 */
const INLINE_LIMIT = 2048;
const INTERN_LIMIT = 96;
/** 版本 2 起记录里可能有 payload 引用。旧版（0.5.6 及以前）页面以版本 1 打开会得到 VersionError，
 * 只会提示本地存储不可用，不会把 {$payload} 当成图片/场景读出后写回主稿。
 * 已打开的旧页面收到 versionchange 后自行关闭连接。v1 → v2 无需转换：内联旧记录照常可读。 */
const DATABASE_VERSION = 2;

export class DraftSupersededError extends Error {
  constructor() { super("DRAFT_SUPERSEDED"); }
}

function valid(value: any): value is SavedDraft {
  return value?.schema === 1 && typeof value.text === "string" && Array.isArray(value.assets);
}

type PayloadRef = {$payload: string};
function isRef(value: any): value is PayloadRef {
  return !!value && typeof value === "object" && !Array.isArray(value) && typeof value.$payload === "string" &&
    Object.keys(value).length === 1;
}
function isPlain(value: any): boolean {
  if (!value || typeof value !== "object") return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null || Array.isArray(value);
}

/** 上传进度/续传票据不是稿件内容；其余字段（含编辑中的场景、画布文字、图注）都要比较。 */
const VOLATILE = new Set(["progress", "upload_ticket"]);
function sameValue(a: any, b: any, top = false): boolean {
  if (a === b) return true;
  // Blob 内容无法同步读出；大小和类型相同不代表像素相同。不同对象一律视为不同（宁可多留一份可恢复副本）。
  if (typeof Blob !== "undefined" && (a instanceof Blob || b instanceof Blob)) return false;
  if (!isPlain(a) || !isPlain(b) || Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) return a.length === b.length && a.every((item, i) => sameValue(item, b[i]));
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) {
    if (top && VOLATILE.has(key)) continue;
    const x = a[key], y = b[key];
    if (x === undefined && y === undefined) continue;
    if (!sameValue(x, y)) return false;
  }
  return true;
}

/** 两份稿件对用户而言是否同一内容：正文与每张图的全部可恢复字段（原图、成品、场景、未保存的编辑）。 */
export function sameContent(a: SavedDraft, b: any): boolean {
  return valid(b) && a.text === b.text && a.assets.length === b.assets.length &&
    a.assets.every((item, i) => sameValue(item, b.assets[i], true));
}

function collectRefs(node: any, out: Set<string>): void {
  if (isRef(node)) { out.add(node.$payload); return; }
  if (!isPlain(node)) return;
  for (const value of Array.isArray(node) ? node : Object.values(node)) collectRefs(value, out);
}

function substitute(node: any, payloads: Map<string, unknown>): any {
  if (isRef(node)) return payloads.get(node.$payload);
  if (!isPlain(node)) return node;
  if (Array.isArray(node)) return node.map(item => substitute(item, payloads));
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(node)) {
    const next = substitute(value, payloads);
    if (next !== undefined) out[key] = next; // 缺失的 payload 不伪造内容；界面据此标为未完成。
  }
  return out;
}

/** 在当前事务内把记录里的 payload 引用换回内容，然后回调（仍在同一事务中）。 */
function resolveInTx(store: IDBObjectStore, raw: any, done: (value: any) => void): void {
  const refs = new Set<string>();
  if (valid(raw)) collectRefs(raw.assets, refs);
  if (!refs.size) { done(raw); return; }
  const payloads = new Map<string, unknown>();
  let left = refs.size;
  for (const key of refs) {
    const request = store.get(key);
    request.onsuccess = () => {
      if (typeof request.result === "string") payloads.set(key, request.result);
      if (--left === 0) done({...raw, assets: substitute(raw.assets, payloads)});
    };
  }
}

function randomTab(): string {
  const bytes = new Uint8Array(8);
  globalThis.crypto?.getRandomValues?.(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("") + Date.now().toString(36);
}

type Packed = {record: SavedDraft; payloads: Map<string, string>};
type Waiter = {seq: number; resolve: () => void; reject: (e: Error) => void};

export class DraftRepository {
  private db: Promise<IDBDatabase> | null = null;
  private pending: Packed | null = null;
  private pendingPlain: SavedDraft | null = null;
  private pendingSeq = 0;
  private requested = 0;
  private running = false;
  private waiters: Waiter[] = [];
  private error: Error | null = null;
  private claimed = false;
  private target = "current";
  /** 内容 → payload 键。同一字符串对象在 V8 中缓存哈希，查询代价与长度无关。 */
  private interned = new Map<string, string>();
  private payloadSerial = 0;
  private payloadsSinceSweep = 0;
  private sweepTimer: ReturnType<typeof setTimeout> | null = null;
  readonly tab: string;
  superseded = false;
  /** 本页最后一次成功写入主稿的内容；用于判断别的页面是否改过主稿。 */
  lastWritten: SavedDraft | null = null;

  constructor(private onState: (state: SaveState) => void = () => {},
              private onSuperseded: () => void = () => {}, tab = randomTab()) {
    this.tab = tab;
  }

  /** 浏览器根本没有 IndexedDB：这不是“读失败”，没有可读旧稿。 */
  static available(): boolean { return !!globalThis.indexedDB; }

  private open(): Promise<IDBDatabase> {
    if (this.db) return this.db;
    this.db = new Promise<IDBDatabase>((resolve, reject) => {
      if (!globalThis.indexedDB) { reject(new Error("LOCAL_STORAGE_UNAVAILABLE")); return; }
      const request = indexedDB.open(DATABASE, DATABASE_VERSION);
      let rejected = false;
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE);
      };
      request.onerror = () => reject(new Error("LOCAL_STORAGE_UNAVAILABLE"));
      request.onblocked = () => { rejected = true; reject(new Error("LOCAL_STORAGE_BLOCKED")); };
      request.onsuccess = () => {
        const db = request.result;
        if (rejected) { db.close(); return; } // 已按被阻塞报告；迟到的连接不留着阻挡别的页面
        db.onversionchange = () => { db.close(); this.db = null; };
        resolve(db);
      };
    }).catch(error => {this.db = null; throw error;});
    return this.db;
  }

  private writeTx(db: IDBDatabase): IDBTransaction {
    try { return db.transaction(STORE, "readwrite", {durability: "strict"}); }
    catch { return db.transaction(STORE, "readwrite"); }
  }

  private payloadKey(content: string): string {
    let key = this.interned.get(content);
    if (key) { this.interned.delete(content); }
    else key = `${PAYLOAD_PREFIX}${this.tab}-${Date.now().toString(36)}-${(++this.payloadSerial).toString(36)}`;
    this.interned.set(content, key);
    while (this.interned.size > INTERN_LIMIT) this.interned.delete(this.interned.keys().next().value!);
    return key;
  }

  /** 生成可直接写入的记录副本：大字符串换成引用，其余字段深拷贝（Blob 不可变，原样保留）。 */
  private pack(value: SavedDraft): Packed {
    const payloads = new Map<string, string>();
    const walk = (node: any, depth: number): any => {
      if (typeof node === "string") {
        if (depth === 0 || node.length <= INLINE_LIMIT) return node;
        const key = this.payloadKey(node);
        payloads.set(key, node);
        return {$payload: key};
      }
      if (!isPlain(node)) return node;
      if (Array.isArray(node)) return node.map(item => walk(item, depth + 1));
      const out: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(node)) if (v !== undefined) out[k] = walk(v, depth + 1);
      return out;
    };
    const record = {...value, assets: walk(value.assets, 0), last_intent: value.last_intent ? {...value.last_intent} : value.last_intent};
    return {record, payloads};
  }

  /** 引用与内容同事务落盘；payload 若已被清理，用内存中的内容补写。 */
  private putPacked(store: IDBObjectStore, key: string, packed: Packed): void {
    for (const [payloadKey, content] of packed.payloads) {
      const count = store.count(payloadKey);
      count.onsuccess = () => { if (!count.result) { store.put(content, payloadKey); this.payloadsSinceSweep++; } };
    }
    store.put(packed.record, key);
  }

  async load(key = "current"): Promise<SavedDraft | null> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const store = tx.objectStore(STORE);
      let value: any = null;
      const request = store.get(key);
      request.onsuccess = () => resolveInTx(store, request.result, resolved => { value = resolved; });
      tx.oncomplete = () => resolve(valid(value) ? value : null);
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_READ_FAILED"));
    });
  }

  /** 读出主稿并登记本页为写入者。读失败抛错（调用方不得当作空稿）；无法识别的旧记录先另存。 */
  async claim(): Promise<SavedDraft | null> {
    const db = await this.open();
    const result = await new Promise<SavedDraft | null>((resolve, reject) => {
      const tx = this.writeTx(db);
      const store = tx.objectStore(STORE);
      let found: SavedDraft | null = null;
      const request = store.get("current");
      request.onsuccess = () => {
        const value = request.result;
        if (valid(value)) resolveInTx(store, value, resolved => { found = resolved; });
        else if (value !== undefined) store.put(value, `unreadable-${Date.now()}`);
        store.put({tab: this.tab, at: Date.now()}, WRITER);
      };
      tx.oncomplete = () => resolve(found);
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_READ_FAILED"));
    });
    this.claimed = true;
    this.superseded = false;
    this.error = null;
    this.target = "current";
    this.lastWritten = result;
    this.scheduleSweep(1500);
    return result;
  }

  /** 读不出主稿时，用户选择先写新稿：写到独立记录，主稿保持原样。 */
  useSideKey(): string {
    this.target = `${SIDE_PREFIX}new-${Date.now().toString(36)}-${this.tab.slice(0, 6)}`;
    return this.target;
  }

  get writingSide(): boolean { return this.target !== "current"; }

  /** 还有写入在途/排队，或上次写入真的失败（不含被接管时的预期拒绝）：内存里的内容未必已落盘。 */
  get unsettled(): boolean {
    return this.running || !!this.pending || (!!this.error && !(this.error instanceof DraftSupersededError));
  }

  async isWriter(): Promise<boolean> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const request = tx.objectStore(STORE).get(WRITER);
      tx.oncomplete = () => resolve(!request.result || request.result.tab === this.tab);
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_READ_FAILED"));
    });
  }

  async sideDrafts(): Promise<SideDraft[]> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const store = tx.objectStore(STORE);
      const out: SideDraft[] = [];
      // 只按键列出，避免把所有图片 payload 读进内存。
      const keys = store.getAllKeys();
      keys.onsuccess = () => {
        for (const raw of keys.result) {
          const key = String(raw);
          if (!key.startsWith(SIDE_PREFIX)) continue;
          const request = store.get(key);
          request.onsuccess = () => {
            const value = request.result;
            if (valid(value) && (value.text || value.assets.length))
              resolveInTx(store, value, draft => out.push({key, draft}));
          };
        }
      };
      tx.oncomplete = () => resolve(out.sort((a, b) => (b.draft.saved_at || 0) - (a.draft.saved_at || 0)));
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_READ_FAILED"));
    });
  }

  async remove(key: string): Promise<void> {
    if (!key.startsWith(SIDE_PREFIX)) return;
    const db = await this.open();
    await new Promise<void>((resolve, reject) => {
      const tx = this.writeTx(db);
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_WRITE_FAILED"));
      tx.objectStore(STORE).delete(key);
    });
    this.scheduleSweep(1500);
  }

  /** 在同一事务内确认本页仍是写入者后执行 puts；否则把被拒快照另存并报告已被接管。 */
  private async guarded(puts: Array<[string, Packed]>, rejected?: {plain: SavedDraft; packed: Packed}): Promise<void> {
    const db = await this.open();
    let lost = false;
    await new Promise<void>((resolve, reject) => {
      const tx = this.writeTx(db);
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_WRITE_FAILED"));
      try {
        const store = tx.objectStore(STORE);
        const apply = () => { for (const [key, value] of puts) this.putPacked(store, key, value); };
        if (!this.claimed) { apply(); return; }
        const writer = store.get(WRITER);
        const current = store.get("current");
        current.onsuccess = () => {
          try {
            if (writer.result && writer.result.tab !== this.tab) {
              lost = true;
              // 与当前主稿相同的快照不必另存；不同的内容（包括未保存的图片编辑）留作「最近」里可恢复的一份。
              if (rejected && (rejected.plain.text || rejected.plain.assets.length)) resolveInTx(store, current.result, main => {
                try { if (!sameContent(rejected.plain, main)) this.putPacked(store, this.supersededKey, rejected.packed); }
                catch { try {tx.abort();} catch {} }
              });
            } else apply();
          } catch { try {tx.abort();} catch {} }
        };
      } catch { try {tx.abort();} catch {} reject(new Error("LOCAL_STORAGE_WRITE_FAILED")); }
    });
    if (lost) {
      const first = !this.superseded;
      this.superseded = true;
      if (first) this.onSuperseded();
      throw new DraftSupersededError();
    }
  }

  private async write(key: string, packed: Packed, plain: SavedDraft): Promise<void> {
    // 所有内容在一个事务中替换；只在 oncomplete 后显示已保存。
    if (key.startsWith(SIDE_PREFIX)) return this.guardedFree(key, packed);
    await this.guarded([[key, packed]], {plain, packed});
    if (key === "current") this.lastWritten = plain;
  }

  get supersededKey(): string { return `${SIDE_PREFIX}superseded-${this.tab}`; }

  /** 被接管时另存的快照已重新成为主稿：只删内容相同的那份，别的内容继续留在「最近」。 */
  async discardSide(key: string, expected: SavedDraft): Promise<void> {
    if (!key.startsWith(SIDE_PREFIX)) return;
    const db = await this.open();
    await new Promise<void>((resolve, reject) => {
      const tx = this.writeTx(db);
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_WRITE_FAILED"));
      const store = tx.objectStore(STORE);
      const request = store.get(key);
      request.onsuccess = () => resolveInTx(store, request.result, side => {
        if (sameContent(expected, side)) store.delete(key);
      });
    });
    this.scheduleSweep(1500);
  }

  private async guardedFree(key: string, value: Packed): Promise<void> {
    const db = await this.open();
    await new Promise<void>((resolve, reject) => {
      const tx = this.writeTx(db);
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_WRITE_FAILED"));
      try { this.putPacked(tx.objectStore(STORE), key, value); }
      catch { try {tx.abort();} catch {} reject(new Error("LOCAL_STORAGE_WRITE_FAILED")); }
    });
  }

  save(value: SavedDraft): void {
    // 合并高频更新，但不把新稿写在旧事务之前。打包即深拷贝，调用方之后修改内存不影响这次写入。
    this.pending = this.pack(value);
    this.pendingPlain = value;
    this.pendingSeq = ++this.requested;
    this.error = null;
    this.onState("saving");
    if (!this.running) void this.drain();
  }

  /** 已被接管后本页的快照只进本页的 side-superseded 记录，绝不写主稿；与主稿相同或为空则不另存。 */
  private async preserveSuperseded(packed: Packed, value: SavedDraft): Promise<void> {
    if (!value.text && !value.assets.length) return;
    const db = await this.open();
    const key = this.supersededKey;
    await new Promise<void>((resolve, reject) => {
      const tx = this.writeTx(db);
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_WRITE_FAILED"));
      try {
        const store = tx.objectStore(STORE);
        const current = store.get("current");
        current.onsuccess = () => resolveInTx(store, current.result, main => {
          try { if (!sameContent(value, main)) this.putPacked(store, key, packed); }
          catch { try {tx.abort();} catch {} }
        });
      } catch { try {tx.abort();} catch {} reject(new Error("LOCAL_STORAGE_WRITE_FAILED")); }
    });
  }

  /** 让等待 seq 及更早快照的调用方得到结果；更晚的快照不拖住更早的等待者。 */
  private settleWaiters(seq: number, error: Error | null): void {
    const ready = this.waiters.filter(w => w.seq <= seq);
    this.waiters = this.waiters.filter(w => w.seq > seq);
    for (const item of ready) error ? item.reject(error) : item.resolve();
  }

  private async drain(): Promise<void> {
    this.running = true;
    while (this.pending) {
      const packed = this.pending, plain = this.pendingPlain!, seq = this.pendingSeq;
      this.pending = null; this.pendingPlain = null;
      try {
        if (this.superseded && this.target === "current") {
          await this.preserveSuperseded(packed, plain);
          this.error = new DraftSupersededError();
          this.onState("superseded");
          this.settleWaiters(seq, this.error);
        } else {
          await this.write(this.target, packed, plain);
          this.error = null;
          this.settleWaiters(seq, null);
        }
      } catch (error) {
        const superseded = error instanceof DraftSupersededError;
        this.error = superseded ? error : new Error("LOCAL_STORAGE_WRITE_FAILED");
        this.onState(superseded ? "superseded" : "unavailable");
        // 被拒期间排队的更新快照（包括 onSuperseded 回调里刚排入的）比被拒的那份更新：
        // 继续循环，下一轮把它另存为本页的 side 记录，而不是丢掉。写失败且没有新快照时停下。
        if (superseded) this.settleWaiters(seq, this.error);
        if (!superseded && !this.pending) break;
      }
    }
    this.running = false;
    if (!this.error) this.onState("saved");
    if (this.payloadsSinceSweep >= 6) this.scheduleSweep(4000);
    // 剩余等待者对应的快照都已处理完：按最终结果答复。
    const waiting = this.waiters.splice(0);
    for (const item of waiting) this.error ? item.reject(this.error) : item.resolve();
  }

  /** 等待调用时刻之前排入的快照落盘（或确定失败）；之后的新编辑不会让它无限等待。 */
  flush(): Promise<void> {
    if (!this.running && !this.pending) return this.error ? Promise.reject(this.error) : Promise.resolve();
    const seq = this.requested;
    return new Promise((resolve, reject) => this.waiters.push({seq, resolve, reject}));
  }

  /** 等此前排队的写入落定以保持顺序；它们先前写失败不阻止本次（本次会写入完整内容），被接管由事务内检查决定。 */
  private async settlePrevious(): Promise<void> {
    try { await this.flush(); } catch { /* 交给本次受保护写入判断 */ }
  }

  async backup(value: SavedDraft): Promise<void> {
    await this.settlePrevious();
    await this.guarded([["before-replace", this.pack(value)]]);
  }

  async replaceWithBackup(before: SavedDraft, after: SavedDraft): Promise<void> {
    await this.settlePrevious();
    // A preceding completion receipt may already have archived this draft.
    // Resetting an empty/stuck state must not erase that recovery copy.
    const packedBefore = this.pack(before);
    const puts: Array<[string, Packed]> = [];
    if (before.text || before.assets.length) puts.push(["before-replace", packedBefore]);
    puts.push([this.target, this.pack(after)]);
    await this.guarded(puts, {plain: before, packed: packedBefore});
    if (this.target === "current") this.lastWritten = after;
    // 主稿已整体替换成功：先前那次失败写入的内容已被取代，存储恢复可用。
    if (!this.running && !this.pending) this.error = null;
    this.onState("saved");
  }

  private scheduleSweep(ms: number): void {
    if (this.sweepTimer !== null || typeof setTimeout !== "function") return;
    this.sweepTimer = setTimeout(() => { this.sweepTimer = null; void this.sweep().catch(() => {}); }, ms);
  }

  /** 删除没有任何稿件记录引用的 payload。读取引用与删除在同一事务；并发写入会在自己的事务里补写 payload。 */
  async sweep(): Promise<number> {
    const db = await this.open();
    let removed = 0;
    await new Promise<void>((resolve, reject) => {
      const tx = this.writeTx(db);
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_WRITE_FAILED"));
      const store = tx.objectStore(STORE);
      const keys = store.getAllKeys();
      keys.onsuccess = () => {
        const all = keys.result.map(String);
        const payloadKeys = all.filter(k => k.startsWith(PAYLOAD_PREFIX));
        if (!payloadKeys.length) return;
        const records = all.filter(k => !k.startsWith(PAYLOAD_PREFIX));
        const used = new Set<string>();
        let left = records.length;
        const finish = () => {
          for (const key of payloadKeys) if (!used.has(key)) { store.delete(key); removed++; }
        };
        if (!left) { finish(); return; }
        for (const key of records) {
          const request = store.get(key);
          request.onsuccess = () => {
            collectRefs(request.result, used);
            if (--left === 0) finish();
          };
        }
      };
    });
    this.payloadsSinceSweep = 0;
    return removed;
  }
}
