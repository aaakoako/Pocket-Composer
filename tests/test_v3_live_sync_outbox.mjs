import assert from 'node:assert/strict';
import {DraftOutbox,buildPrimaryUpdate} from '../web/src/sync.js';
const tick=()=>new Promise(r=>setTimeout(r,0));
const draft=()=>({draft_id:'d',epoch:'e',revision:0,generation:0,text:'',assets:[]});

// 等待 ACK 时就把下一份快照落盘；ACK 一到立即发出，不再串行“写盘+往返”。
{
  const sent=[];const saves=[];
  const outbox=new DraftOutbox({send:m=>sent.push(m),persist:()=>new Promise(r=>saves.push(r))});
  const s=draft();outbox.connect();
  s.text='一';outbox.offer(buildPrimaryUpdate(s,'u1'));
  saves.shift()();await tick();assert.equal(sent.length,1);
  s.text='一二';outbox.offer(buildPrimaryUpdate(s,'u2'));
  assert.equal(saves.length,1,'在途期间开始落盘下一份');
  saves.shift()();await tick();assert.equal(sent.length,1,'未 ACK 前不发第二份');
  assert.equal(outbox.ready.update_id,'u2');
  outbox.acknowledge({...sent[0],durable:true});
  assert.equal(sent.length,2,'ACK 后同步发出已落盘快照');assert.equal(sent[1].update_id,'u2');
  outbox.acknowledge({...sent[1],durable:true});await outbox.flush('u2');
  // 已落盘待发的快照不跨连接发送。
  s.text='一二三';outbox.offer(buildPrimaryUpdate(s,'u3'));saves.shift()();await tick();
  s.text='一二三四';outbox.offer(buildPrimaryUpdate(s,'u4'));saves.shift()();await tick();
  assert.equal(outbox.ready.update_id,'u4');
  outbox.disconnect();assert.equal(outbox.ready,null);
  outbox.connect();await tick();
  assert.equal(saves.length,1);saves.shift()();await tick();
  assert.equal(sent.at(-1).update_id,'u4');assert.equal(sent.filter(m=>m.update_id==='u4').length,1);
  outbox.close();
}
// 写盘失败不进入重试死循环，也不发送未落盘的快照。
{
  const sent=[];let persists=0;
  const outbox=new DraftOutbox({send:m=>sent.push(m),persist:async()=>{persists++;throw new Error('quota')}});
  const states=[];outbox.onState=s=>states.push(s);
  const s=draft();outbox.connect();s.text='x';outbox.offer(buildPrimaryUpdate(s,'f1'));
  await tick();await tick();
  assert.equal(sent.length,0);assert.equal(persists,1);assert.equal(states.at(-1),'save_failed');
  outbox.close();
}
// 快速连续输入：发送间隔受限，间隔内只保留最新一份；最后一份一定发出。限速拒收退回重发，不停止同步。
{
  let clock=0;const timers=[];const sent=[];const states=[];
  const timer=(fn,ms)=>{const t={at:clock+ms,fn};timers.push(t);return t};
  const cancel=t=>{const i=timers.indexOf(t);if(i>=0)timers.splice(i,1)};
  const advance=async ms=>{const end=clock+ms;for(;;){timers.sort((a,b)=>a.at-b.at);const t=timers[0];
    if(!t||t.at>end)break;timers.shift();clock=t.at;t.fn();await tick();}clock=end;await tick();};
  const outbox=new DraftOutbox({send:m=>sent.push(m),persist:async()=>{},timer,cancel,now:()=>clock,
    minIntervalMs:100,backoffMs:1000,retryMs:1600,onState:s=>states.push(s)});
  const s=draft();outbox.connect();
  const ackLast=()=>outbox.acknowledge({...sent.at(-1),durable:true});
  s.text='a';outbox.offer(buildPrimaryUpdate(s,'r0'));await tick();
  assert.equal(sent.length,1,'空闲后第一份立即发出');ackLast();
  for(let i=1;i<=50;i++){s.text+='字';outbox.offer(buildPrimaryUpdate(s,'r'+i));await advance(20);
    if(outbox.flight)ackLast();}
  assert.ok(sent.length<=12,`1 秒内最多约 10 份，实际 ${sent.length}`);
  await advance(200);if(outbox.flight)ackLast();await advance(200);
  assert.equal(sent.at(-1).update_id,'r50','最后一份必定发出');assert.equal(sent.at(-1).text,s.text);
  // 电脑仍回限速：退回待发，退避后重发同一份，不进入冲突。
  s.text+='尾';outbox.offer(buildPrimaryUpdate(s,'r51'));await advance(150);
  assert.equal(sent.at(-1).update_id,'r51');
  assert.equal(outbox.defer({type:'error',error:'rate limited',update_id:'r51'}),true);
  assert.equal(outbox.failure,null);assert.equal(states.at(-1),'retrying');
  const before=sent.length;await advance(500);assert.equal(sent.length,before,'退避期间不重发');
  await advance(600);assert.equal(sent.at(-1).update_id,'r51');ackLast();await advance(10);
  await outbox.flush('r51');
  // 与草稿无关的错误（如心跳被限速）不停止同步。
  outbox.reject({type:'error',error:'rate limited'});assert.equal(outbox.failure,null);
  outbox.close();
}
console.log('live outbox ok');
