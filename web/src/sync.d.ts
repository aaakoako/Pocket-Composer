export type SyncAsset = { id?: string; asset_id?: string; status?: string; render_revision?: number; caption?: string; [key: string]: unknown };
export type SyncState = { text: string; assets: any[]; draft_id: string; epoch: string; revision: number; conflict?: any };
export function receiptMatches(state: SyncState, archived: any): boolean;
export function applyRotated(state: SyncState, msg: any): "cleared" | "kept";
export function applyReady(state: SyncState, msg: any): "same" | "adopt" | "conflict";

export function buildDraftUpdate(state: SyncState): Record<string, any>;
export function buildPrimaryUpdate(state: SyncState & {generation?: number}, updateId: string): Record<string,any>;
export function rotatePrimary(state: SyncState & {generation?: number}, msg:any, makeId:()=>string): "kept"|"cleared";
export class DraftOutbox {
  constructor(options: {send:(message:any)=>void; persist:()=>Promise<void>; onState?:(state:string)=>void;
    timer?:(fn:()=>void,ms:number)=>any;cancel?:(id:any)=>void;retryMs?:number;debounceMs?:number;
    minIntervalMs?:number;backoffMs?:number;now?:()=>number});
  latest: any; flight: any; ready: any; acked: string|null; online: boolean; failure: string|null;
  offer(message:any):void;connect():void;disconnect():void;close():void;
  acknowledge(ack:any):boolean;reject(ack:any):void;defer(ack:any):boolean;
  flush(id?:string,timeoutMs?:number):Promise<void>;
}
