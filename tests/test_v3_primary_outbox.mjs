import assert from 'node:assert/strict';
import {DraftOutbox,buildPrimaryUpdate,rotatePrimary} from '../web/src/sync.js';
const tick=()=>new Promise(r=>setTimeout(r,0));
const draft=()=>({draft_id:'d',epoch:'e',revision:0,generation:0,text:'A',assets:[]});
const calls=[]; const jobs=new Map();let i=0;
const saved=[];
const q=new DraftOutbox({send:m=>calls.push(m),persist:async()=>saved.push('saved'),timer:(f,_ms)=>{jobs.set(++i,f);return i},cancel:id=>jobs.delete(id)});
const state=draft();q.offer(buildPrimaryUpdate(state,'m1'));assert.equal(calls.length,0);
q.connect();await tick();assert.equal(calls.length,1);assert.equal(saved.length,1);
state.text='B';q.offer(buildPrimaryUpdate(state,'m2'));state.text='C';q.offer(buildPrimaryUpdate(state,'m3'));
assert.equal(calls.length,1);assert.equal(q.latest.text,'C');
assert.equal(q.acknowledge({...calls[0],update_id:'wrong',durable:true}),false);
[...jobs.values()][0]();assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);
// 等待 ACK 期间已落盘的 B 先发出（流水线），随后合并到最新的 C。
q.acknowledge({...calls[0],durable:true});await tick();assert.equal(calls.length,3);assert.equal(calls[2].text,'B');
q.acknowledge({...calls[2],durable:true});await tick();assert.equal(calls.length,4);assert.equal(calls[3].text,'C');
q.acknowledge({...calls[3],durable:true});await q.flush('m3');
q.disconnect();state.text='离线';q.offer(buildPrimaryUpdate(state,'m4'));q.connect();await tick();assert.equal(calls.at(-1).text,'离线');q.close();
const s=draft();s.revision=3;
const receipt={phone_primary:true,rotated:true,generation:0,archived:{draft_id:'d',epoch:'e',revision:3,source_text:'A',asset_refs:[],assets:[]}};
assert.equal(rotatePrimary(s,receipt,()=> 'phone-next'),'cleared');assert.equal(s.epoch,'phone-next');assert.equal(s.generation,1);
assert.equal(rotatePrimary(s,receipt,()=> 'bad'),'kept');
const b=draft();b.revision=3;b.assets=[{id:'new',status:'editing'}];assert.equal(rotatePrimary(b,receipt,()=> 'bad'),'kept');assert.equal(b.assets.length,1);
console.log('primary outbox: coalescing, idempotent retry, matching ACK, offline continuation, safe local rotation passed');

// 制作很快的小图片也必须先宣布未完成版本；不能等防抖结束直接显示成品。
{
  const sent=[]; const pending=[];
  const outbox=new DraftOutbox({send:m=>sent.push(m),persist:async()=>{},debounceMs:1,
    timer:(fn,ms)=>{const id=setTimeout(fn,ms===1?15:ms);pending.push(id);return id;}});
  const imageState=draft();
  imageState.assets=[{id:'photo',status:'queued',render_revision:2}];
  outbox.offer(buildPrimaryUpdate(imageState,'queued-image'));outbox.connect();
  imageState.assets[0]={id:'photo',asset_id:'rendered-id',status:'ready',render_revision:2};
  outbox.offer(buildPrimaryUpdate(imageState,'ready-image'));
  await new Promise(r=>setTimeout(r,30));
  assert.equal(sent.length,1);
  assert.equal(sent[0].asset_documents[0].status,'queued');
  assert.equal(outbox.latest.asset_documents[0].status,'ready');
  outbox.acknowledge({...sent[0],durable:true});
  await new Promise(r=>setTimeout(r,30));
  assert.equal(sent.length,2);
  assert.equal(sent[1].asset_documents[0].status,'ready');
  outbox.acknowledge({...sent[1],durable:true});
  await outbox.flush('ready-image');outbox.close();pending.forEach(clearTimeout);
}
// 写盘过程中继续说话，首个已排快照不能饥饿；收到ACK后合并到最新句子。
{
  const sent=[];let finishSave;
  const outbox=new DraftOutbox({send:m=>sent.push(m),persist:()=>new Promise(r=>{finishSave=r;})});
  const input=draft();outbox.offer(buildPrimaryUpdate(input,'speech-1'));outbox.connect();
  for(let n=2;n<=20;n++){input.text='连续说话 '+n;outbox.offer(buildPrimaryUpdate(input,'speech-'+n));}
  finishSave();await tick();
  assert.equal(sent.length,1);assert.equal(sent[0].update_id,'speech-1');
  outbox.acknowledge({...sent[0],durable:true});finishSave();await tick();
  assert.equal(sent.length,2);assert.equal(sent[1].text,'连续说话 20');
  outbox.acknowledge({...sent[1],durable:true});await outbox.flush('speech-20');outbox.close();
}
console.log('primary outbox: pending image visibility and continuous input during persistence passed');

// 断开并重连发生在写盘过程中，只发送新连接的最新稿，不让旧待发占住队列。
{
  const sent=[];let finishSave;
  const outbox=new DraftOutbox({send:m=>sent.push(m),persist:()=>new Promise(r=>{finishSave=r;})});
  const input=draft();outbox.offer(buildPrimaryUpdate(input,'old-session'));outbox.connect();
  outbox.disconnect();input.text='离线后新稿';outbox.offer(buildPrimaryUpdate(input,'new-session'));outbox.connect();
  finishSave();await tick();assert.equal(sent.length,0);
  finishSave();await tick();assert.equal(sent.length,1);assert.equal(sent[0].update_id,'new-session');
  outbox.acknowledge({...sent[0],durable:true});await outbox.flush('new-session');outbox.close();
}
console.log('primary outbox: reconnect during local persistence never sends the old session snapshot');
