import React, {useEffect, useState} from 'react';

export default function GuidedFirstFrames({pid, shot, state, call, run, refresh}) {
  const root=`/projects/${encodeURIComponent(pid)}/guided`;
  const [frames,setFrames]=useState([]),[budget,setBudget]=useState('1'),[preview,setPreview]=useState(null);
  const [provider,setProvider]=useState(state.video_model?.provider || 'minimax');
  useEffect(()=>{setPreview(null);if(shot)call(root+`/shots/${shot.seq}/frames`).then(setFrames).catch(()=>setFrames([]));},[pid,shot?.seq,state.revision]);
  useEffect(()=>{if(state.video_model)setProvider(state.video_model.provider);},[state.video_model?.provider]);
  if(!shot)return null;
  return <section className="guided-card"><h4>首帧 · 镜头 {shot.seq}</h4>
    <p>先确认首帧，之后才能用支持首帧的模型生成视频。每次图片调用须单独授权；已生成的版本会保留。</p>
    <div className="guided-image-grid">{frames.map(f=><figure key={f.id} className={f.confirmed?'selected':''}>
      <img src={`/api${root}/images/${f.id}`} alt={`镜头 ${shot.seq} 首帧候选`}/>
      <figcaption>{f.stale?'基于旧版本 · ':''}{f.confirmed?'已确认 · ':''}{f.id.slice(0,6)}</figcaption>
      <figcaption>角色图：{f.references.length?f.references.map(r=>r.image_id.slice(0,6)).join('、'):'无'}</figcaption>
      <button onClick={()=>run(async()=>{if(f.stale&&!window.confirm('这张首帧基于旧角色版本，确认继续使用？'))return;
        await call(root+`/shots/${shot.seq}/frames/${f.id}/confirm`,'POST',{revision:state.revision});await refresh(true);})}>{f.confirmed?'重新确认':'确认此首帧'}</button>
    </figure>)}</div>
    <div className="fields"><label>本次首帧预算 USD<input type="number" min="0" step="0.01" value={budget} onChange={e=>setBudget(e.target.value)}/></label>
      <button onClick={()=>run(async()=>setPreview(await call(root+`/shots/${shot.seq}/frames/preview`,'POST',{revision:state.revision})))}>Dry Run / 首帧预览</button></div>
    {preview&&<><p>将发送镜头提示词及 {preview.references.length} 张已确认的角色参考图；价格未知，预算不是供应商硬上限。</p>
      <button disabled={Number(budget)<=0} onClick={()=>run(async()=>{if(!window.confirm(`确认付费生成镜头 ${shot.seq} 的首帧？预算 $${budget}，实际账单可能不同。`))return;
        const r=await call(root+`/shots/${shot.seq}/frames`,'POST',{revision:state.revision,budget_usd:Number(budget),confirm_paid:true,request_id:crypto.randomUUID()});
        setPreview(null);await refresh(true);if(r.status!=='COMPLETED')throw Error('首帧请求结果不明，请先核查供应商账单。');})}>授权生成首帧</button></>}
    <div className="fields"><label>首帧视频模型<select value={provider} onChange={e=>setProvider(e.target.value)}>
      <option value="minimax">MiniMax-H3 · 图生视频</option><option value="mock">Mock · 本地测试</option><option value="flux" disabled>Flux · 图片输入方式待核实</option>
    </select></label><button onClick={()=>run(async()=>{const model=provider==='mock'?'mock':'MiniMax-H3';
      await call(root+'/video-model','PUT',{provider,model,revision:state.revision});await refresh(true);})}>选择视频模型</button></div>
    {state.video_model&&<p>当前视频模型：{state.video_model.provider} / {state.video_model.model}</p>}
    {shot.old_video_decision_required&&<div className="guided-warning">此镜头已有视频基于旧版本或来源未知。请决定是否继续使用旧视频。
      <button disabled={!shot.asset_id} onClick={()=>run(async()=>{if(!window.confirm('确认继续使用当前旧视频？它不会被改写。'))return;
        await call(root+`/shots/${shot.seq}/keep-old-video`,'POST',{asset_id:shot.asset_id,revision:state.revision});await refresh(true);})}>继续使用当前旧视频</button></div>}
  </section>;
}
