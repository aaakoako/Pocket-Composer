/** 手机工作稿：图片像素与场景存 IndexedDB，不把大图塞进 sessionStorage。
 * 每次写入同一事务；失败保留内存及上次成功记录，并由界面明确提示。
 * 凭据不在本模块中保存。浏览器清理/私密模式仍可能移除数据，不承诺永久保存。
 *
 * 同一手机可能开着多个标签页：claim() 把本页登记为当前写入者（同一事务读出主稿）。
 * 之后若别的页面接管，本页对主稿的写入在事务内被拒绝，被拒的快照另存为 side-* 记录。
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

export class DraftSupersededError extends Error {
  constructor() { super("DRAFT_SUPERSEDED"); }
}

function valid(value: any): value is SavedDraft {
  return value?.schema === 1 && typeof value.text === "string" && Array.isArray(value.assets);
}

function assetKey(a: any): string {
  return JSON.stringify([a?.id, a?.asset_id, a?.render_revision || 1, a?.caption || ""]);
}

export function sameContent(a: SavedDraft, b: any): boolean {
  return valid(b) && a.text === b.text && a.assets.length === b.assets.length &&
    a.assets.every((item, i) => assetKey(item) === assetKey(b.assets[i]));
}

function randomTab(): string {
  const bytes = new Uint8Array(8);
  globalThis.crypto?.getRandomValues?.(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("") + Date.now().toString(36);
}

export class DraftRepository {
  private db: Promise<IDBDatabase> | null = null;
  private pending: SavedDraft | null = null;
  private running = false;
  private waiters: Array<{resolve: () => void; reject: (e: Error) => void}> = [];
  private error: Error | null = null;
  private claimed = false;
  private target = "current";
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
      const request = indexedDB.open(DATABASE, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE);
      };
      request.onerror = () => reject(new Error("LOCAL_STORAGE_UNAVAILABLE"));
      request.onblocked = () => reject(new Error("LOCAL_STORAGE_BLOCKED"));
      request.onsuccess = () => {
        const db = request.result;
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

  async load(key = "current"): Promise<SavedDraft | null> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const request = tx.objectStore(STORE).get(key);
      tx.oncomplete = () => {
        const value = request.result;
        resolve(valid(value) ? value : null);
      };
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
        if (valid(value)) found = value;
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
      const out: SideDraft[] = [];
      const request = tx.objectStore(STORE).openCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        const key = String(cursor.key);
        if (key.startsWith(SIDE_PREFIX) && valid(cursor.value) && (cursor.value.text || cursor.value.assets.length))
          out.push({key, draft: cursor.value});
        cursor.continue();
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
  }

  /** 在同一事务内确认本页仍是写入者后执行 puts；否则把被拒快照另存并报告已被接管。 */
  private async guarded(puts: Array<[string, unknown]>, rejected?: SavedDraft): Promise<void> {
    const db = await this.open();
    let lost = false;
    await new Promise<void>((resolve, reject) => {
      const tx = this.writeTx(db);
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_WRITE_FAILED"));
      try {
        const store = tx.objectStore(STORE);
        const apply = () => { for (const [key, value] of puts) store.put(value, key); };
        if (!this.claimed) { apply(); return; }
        const writer = store.get(WRITER);
        const current = store.get("current");
        current.onsuccess = () => {
          try {
            if (writer.result && writer.result.tab !== this.tab) {
              lost = true;
              // 与当前主稿相同的快照不必另存；不同的内容留作「最近」里可恢复的一份。
              if (rejected && (rejected.text || rejected.assets.length) && !sameContent(rejected, current.result))
                store.put(rejected, this.supersededKey);
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

  private async write(key: string, value: SavedDraft): Promise<void> {
    // 所有内容在一个事务中替换；只在 oncomplete 后显示已保存。
    if (key.startsWith(SIDE_PREFIX)) return this.guardedFree(key, value);
    await this.guarded([[key, value]], value);
    if (key === "current") this.lastWritten = value;
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
      request.onsuccess = () => { if (sameContent(expected, request.result)) store.delete(key); };
    });
  }

  private async guardedFree(key: string, value: SavedDraft): Promise<void> {
    const db = await this.open();
    await new Promise<void>((resolve, reject) => {
      const tx = this.writeTx(db);
      tx.oncomplete = () => resolve();
      tx.onabort = tx.onerror = () => reject(new Error("LOCAL_STORAGE_WRITE_FAILED"));
      try { tx.objectStore(STORE).put(value, key); }
      catch { try {tx.abort();} catch {} reject(new Error("LOCAL_STORAGE_WRITE_FAILED")); }
    });
  }

  save(value: SavedDraft): void {
    this.pending = structuredClone(value); // 合并高频更新，但不把新稿写在旧事务之前。
    this.error = null;
    this.onState("saving");
    if (!this.running) void this.drain();
  }

  /** 已被接管后本页的快照只进本页的 side-superseded 记录，绝不写主稿；与主稿相同或为空则不另存。 */
  private async preserveSuperseded(value: SavedDraft): Promise<void> {
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
        current.onsuccess = () => {
          try { if (!sameContent(value, current.result)) store.put(value, key); }
          catch { try {tx.abort();} catch {} }
        };
      } catch { try {tx.abort();} catch {} reject(new Error("LOCAL_STORAGE_WRITE_FAILED")); }
    });
  }

  private async drain(): Promise<void> {
    this.running = true;
    while (this.pending) {
      const value = this.pending;
      this.pending = null;
      try {
        if (this.superseded && this.target === "current") {
          await this.preserveSuperseded(value);
          this.error = new DraftSupersededError();
          this.onState("superseded");
        } else {
          await this.write(this.target, value);
          this.error = null;
        }
      } catch (error) {
        const superseded = error instanceof DraftSupersededError;
        this.error = superseded ? error : new Error("LOCAL_STORAGE_WRITE_FAILED");
        this.onState(superseded ? "superseded" : "unavailable");
        // 被拒期间排队的更新快照（包括 onSuperseded 回调里刚排入的）比被拒的那份更新：
        // 继续循环，下一轮把它另存为本页的 side 记录，而不是丢掉。写失败且没有新快照时停下。
        if (!superseded && !this.pending) break;
      }
    }
    this.running = false;
    if (!this.error) this.onState("saved");
    const waiting = this.waiters.splice(0);
    for (const item of waiting) this.error ? item.reject(this.error) : item.resolve();
  }

  flush(): Promise<void> {
    if (!this.running && !this.pending) return this.error ? Promise.reject(this.error) : Promise.resolve();
    return new Promise((resolve, reject) => this.waiters.push({resolve, reject}));
  }

  async backup(value: SavedDraft): Promise<void> {
    await this.flush();
    await this.guarded([["before-replace", value]]);
  }

  async replaceWithBackup(before: SavedDraft, after: SavedDraft): Promise<void> {
    await this.flush();
    // A preceding completion receipt may already have archived this draft.
    // Resetting an empty/stuck state must not erase that recovery copy.
    const puts: Array<[string, unknown]> = [];
    if (before.text || before.assets.length) puts.push(["before-replace", before]);
    puts.push([this.target, after]);
    await this.guarded(puts, before);
    if (this.target === "current") this.lastWritten = after;
    this.onState("saved");
  }
}
