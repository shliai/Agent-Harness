const selSet=new Set();
let bulkMode=false,lastSessIds=[],__liveSid=null;

function setBulk(on){
  bulkMode=on;
  $("side").classList.toggle("bulk",on);
  $("bulkBtn").classList.toggle("on",on);
  $("bulkBtn").textContent=on?"完成":"管理";
  const bar=$("batchBar");
  bar.style.display=on?"flex":"none";
  if(!on){
    selSet.clear();
    document.querySelectorAll(".selc").forEach(c=>c.checked=false);
  }
  renderBatchBar();
}
$("bulkBtn").addEventListener("click",()=>setBulk(!bulkMode));
$("bulkExit").addEventListener("click",()=>setBulk(false));

$("selAllChk").addEventListener("change",e=>{
  const on=e.target.checked;
  lastSessIds.forEach(id=>{
    const cb=document.querySelector('.selc[data-id="'+id+'"]');
    if(cb&&cb.disabled)return;               // 生成中的会话不可选
    on?selSet.add(id):selSet.delete(id);
  });
  document.querySelectorAll(".selc").forEach(c=>{
    if(!c.disabled)c.checked=on;
  });
  renderBatchBar();
  e.target.checked=on;
});

$("delSelBtn").addEventListener("click",async()=>{
  if(!selSet.size){toast("请先勾选要删除的会话");return}
  // 安全兜底：正在生成的会话自动跳过
  const gen=(live&&isStreaming)?live.sid:null;
  const ids=[...selSet].filter(x=>x!==gen);
  const skippedGen=selSet.size-ids.length;
  if(!ids.length){toast("所选会话均在生成中，已跳过","err");return}
  if(!confirm("确定删除所选 "+ids.length+" 个会话？此操作不可恢复！"))return;
  try{
    const r=await fetch("/api/sessions/batch-delete",{method:"POST",
      headers:xuid({"Content-Type":"application/json"}),
      body:JSON.stringify({ids})});
    const d=await r.json();
    const del=(d.deleted||[]).length;
    toast("已删除 "+del+" 个"+(skippedGen?("，跳过生成中 "+skippedGen+" 个"):""), del?"ok":"err");
    if((d.deleted||[]).includes(sessionId)){ctrl&&ctrl.abort();fresh()}
    setBulk(false);
    loadSess();
  }catch(e){toast("请求失败","err")}
});

function renderBatchBar(){
  $("selCnt").textContent="已选 "+selSet.size;
  $("selAllChk").checked=lastSessIds.length>0&&selSet.size===lastSessIds.filter(id=>{
    const cb=document.querySelector('.selc[data-id="'+id+'"]');
    return !cb||!cb.disabled;
  }).length;
}

async function loadSess(){
