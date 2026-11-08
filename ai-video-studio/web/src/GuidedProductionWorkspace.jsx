import React, {useEffect, useState} from 'react';
import {accessibleShot, validateDraft, castComplete as isCastComplete, frameVideoReady} from './guidedHelpers.js';
import GuidedCharacters from './GuidedCharacters.jsx';
import GuidedFirstFrames from './GuidedFirstFrames.jsx';

const call = async (path, method = 'GET', body) => {
  const response = await fetch('/api' + path, {method, headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail));
  return value;
};

function CastReview({shot,roles,state,root,call,run,refresh}) {
  const [ids,setIds]=useState(shot.character_ids||[]);
  useEffect(()=>setIds(shot.character_ids||[]),[shot.id,JSON.stringify(shot.character_ids)]);
  return <article className="guided-attempt"><strong>镜头 {shot.seq}</strong> · {shot.cast_reviewed?'已检查':'待检查'}
    {roles.map(c=><label className="guided-cast-option" key={c.id}><input type="checkbox" checked={ids.includes(c.id)}
      onChange={e=>setIds(old=>e.target.checked?[...old,c.id]:old.filter(id=>id!==c.id))}/>{c.name}</label>)}
    <button onClick={()=>run(async()=>{await call(root+`/shots/${shot.seq}/characters`,'PUT',{character_ids:ids,revision:state.revision});await refresh(true);})}>保存角色关联（可为空）</button>
  </article>;
}

export default function GuidedProductionWorkspace({pid, onPlanned}) {
  const [state, setState] = useState(null), [project, setProject] = useState(null), [draft, setDraft] = useState(null);
  const [assets, setAssets] = useState([]), [attempts, setAttempts] = useState([]), [planAttempts, setPlanAttempts] = useState([]);
  const [model, setModel] = useState('gpt-4o-mini'), [budget, setBudget] = useState('1'), [videoBudget, setVideoBudget] = useState('1');
  const [planPreview, setPlanPreview] = useState(null), [shotPreview, setShotPreview] = useState(null);
  const [seq, setSeq] = useState(null), [prompt, setPrompt] = useState(''), [remote, setRemote] = useState('');
  const [busy, setBusy] = useState(false), [message, setMessage] = useState(''), [render, setRender] = useState(null);
  const [castState, setCastState] = useState(null);
  const root = `/projects/${encodeURIComponent(pid)}/guided`;
  const refresh = async (preserveDraft = false) => {
    const [s, p, a, jobs, plans] = await Promise.all([
      call(root + '/'), call(`/projects/${encodeURIComponent(pid)}`), call(`/projects/${encodeURIComponent(pid)}/assets`),
      call(root + '/attempts'), call(root + '/plan/attempts')]);
    setState(s); setProject(p.project); if(!preserveDraft)setDraft(s.draft); setAssets(a); setAttempts(jobs); setPlanAttempts(plans);
    setSeq(old => old && s.shots.some(x => x.seq === old) ? old : (s.shots.find(x => !x.confirmed)?.seq || s.shots[0]?.seq || null));
    return s;
  };
  useEffect(() => {setState(null); setDraft(null); setSeq(null); setPlanPreview(null); setShotPreview(null); setRender(null);
    refresh().catch(e => setMessage(e.message));}, [pid]);
  const shot = state?.shots.find(s => s.seq === seq);
  useEffect(() => {setPrompt(shot?.prompt || ''); setShotPreview(null);}, [pid, seq, shot?.prompt]);
  useEffect(() => {if (!render || ['completed','failed'].includes(render.status)) return;
    const timer = setInterval(() => call(`/projects/${encodeURIComponent(pid)}/renders/${render.id}`).then(setRender).catch(e => setMessage(e.message)), 1500);
    return () => clearInterval(timer);}, [pid, render?.id, render?.status]);
  const run = async fn => {setBusy(true); setMessage(''); try {await fn();} catch (e) {setMessage(e.message); try {
    const [jobs, plans] = await Promise.all([call(root + '/attempts'), call(root + '/plan/attempts')]);
    setAttempts(jobs); setPlanAttempts(plans);
  } catch {}}
    finally {setBusy(false);}};
  const revise = (index, field, value) => setDraft(d => ({...d, shots: d.shots.map((s,i) => i === index ? {...s,[field]:value} : s)}));
  const saveDraft = async () => {const result = await call(root + '/draft', 'PUT', {draft, revision:state.revision});
    setState(result); setDraft(result.draft); await refresh(true); return result;};
  const errors = draft && project ? validateDraft(draft, project.duration, planPreview?.allowed_durations) : '';
  const total = draft?.shots?.reduce((n,s) => n + (Number(s.duration) || 0), 0) || 0;
  const shotsAssets = assets.filter(a => a.seq === seq);
  const currentAttempts = attempts.filter(a => a.shot_id === shot?.id);
  const allConfirmed = !!state?.shots.length && state.shots.every(s => s.confirmed && !s.old_video_decision_required)
    && (!state?.character_stage_enabled || state.storyboard_reconfirmed);
  const allRolesConfirmed = !!castState && (castState.characters.length ? castState.characters.every(c=>c.confirmed) : castState.empty_cast_confirmed);
  const castComplete = isCastComplete(state?.character_stage_enabled, castState, draft);
  if (!state) return <section className="guided-workspace"><h2>引导式制作</h2><p>{message || '加载中…'}</p></section>;
  return <section className="guided-workspace"><h2>引导式制作 · LLM 分镜</h2>
    <p>① 故事与分镜 → ② 角色定妆 → ③ 分镜确认 → ④ 首帧与视频 → ⑤ 渲染</p>
    {!state.storyboard_confirmed && <>
      <h3>② LLM 分镜</h3><p>项目总时长 {project?.duration} 秒。模型按内容决定镜头数量与时长；费用预留不是供应商硬上限。</p>
      <div className="fields"><label>LLM 模型<input value={model} onChange={e=>{setModel(e.target.value);setPlanPreview(null);}} /></label>
        <label>本次预算 USD<input type="number" min="0" step="0.01" value={budget} onChange={e=>setBudget(e.target.value)} /></label>
        <button disabled={busy} onClick={()=>run(async()=>setPlanPreview(await call(root+'/plan/preview','POST',{model})))}>Dry Run / 检查约束</button></div>
      {planPreview && <><p>允许时长：{planPreview.allowed_durations.join('、')} 秒；LLM 价格：未知，请先核实供应商费率。故事内容将发送给 LLM。</p>
        <button disabled={busy || Number(budget)<=0} onClick={()=>run(async()=>{
          if(!window.confirm(`确认将故事发送给 ${model} 进行付费分镜？\n本次预算预留 $${budget}，不是供应商账单硬上限。`)) return;
          await call(root+'/plan','POST',{model,budget_usd:Number(budget),confirm_paid:true,revision:state.revision,request_id:crypto.randomUUID()});
          await refresh(); onPlanned?.();})}>授权并生成分镜</button></>}
      {planAttempts.filter(a=>['SUBMITTING','UNKNOWN'].includes(a.status)).map(a=><div className="guided-warning" key={a.request_id}>
        上次 LLM 请求结果不明，可能已经计费：{a.error || a.status}。请先核查供应商账单。
        <button disabled={busy} onClick={()=>run(async()=>{if(!window.confirm('确认已核查此请求，允许以后重新授权？'))return;
          await call(root+`/plan/attempts/${encodeURIComponent(a.request_id)}/resolve`,'POST'); await refresh();})}>已核查，解除阻止</button></div>)}
      <GuidedCharacters pid={pid} state={state} call={call} run={run} refresh={refresh} onRoles={setCastState}/>
      {draft && <><h3>分镜审阅 · {total} / {project?.duration} 秒</h3>
        {draft.shots.map((s,i)=><article className="guided-card" key={i}><h4>镜头 {i+1}</h4>
          <label>镜头描述<textarea rows="3" value={s.prompt} onChange={e=>revise(i,'prompt',e.target.value)} /></label>
          <div className="fields"><label>时长（秒）<input type="number" min="1" value={s.duration} onChange={e=>revise(i,'duration',Number(e.target.value))} /></label>
          <button disabled={busy} onClick={()=>setDraft(d=>({...d,shots:d.shots.filter((_,n)=>n!==i)}))}>删除镜头</button></div>
          {state.character_stage_enabled&&<fieldset><legend>出场角色 {Array.isArray(s.character_ids)?'':'· 未确认'}</legend>
            {(castState?.characters||[]).map(c=><label className="guided-cast-option" key={c.id}><input type="checkbox" checked={(s.character_ids||[]).includes(c.id)} onChange={e=>{
              const ids=s.character_ids||[];revise(i,'character_ids',e.target.checked?[...ids,c.id]:ids.filter(id=>id!==c.id));}}/>{c.name}</label>)}
            <button type="button" onClick={()=>revise(i,'character_ids',[])}>此镜头无角色</button></fieldset>}</article>)}
        <button disabled={busy} onClick={()=>setDraft(d=>({...d,shots:[...d.shots,{prompt:'',duration:1}]}))}>＋ 添加镜头</button>{' '}
        <button disabled={busy} onClick={()=>run(saveDraft)}>保存草稿</button>{' '}
        <button disabled={busy || !!errors || !castComplete} onClick={()=>run(async()=>{const saved = await saveDraft(); const next = saved.revision;
          await call(root+'/storyboard/confirm','POST',{revision:next}); await refresh(true); onPlanned?.();})}>确认整份分镜</button>
        {errors && <p className="guided-warning">{errors}</p>}</>}
    </>}
    {state.storyboard_confirmed&&<GuidedCharacters pid={pid} state={state} call={call} run={run} refresh={refresh} onRoles={setCastState}/>}
    {state.storyboard_confirmed&&state.character_stage_enabled&&!state.storyboard_reconfirmed&&<section className="guided-card"><h3>检查已有镜头的出场角色</h3>
      <p>请逐镜保存角色关联；来源不明的旧视频会保留，不能假定使用了新定妆。</p>
      {state.shots.map(s=><CastReview key={s.id} shot={s} roles={castState?.characters||[]} state={state} root={root} call={call} run={run} refresh={refresh}/>)}
      <button disabled={busy||!allRolesConfirmed||state.shots.some(s=>!s.cast_reviewed)} onClick={()=>run(async()=>{await call(root+'/storyboard/reconfirm','POST',{revision:state.revision});await refresh(true);})}>确认分镜与角色关联</button></section>}
    {state.storyboard_confirmed && <><h3>③ 逐镜制作</h3><div className="fields">
      {state.shots.map(s=><button key={s.id} disabled={busy || !accessibleShot(state.shots,s.seq)} onClick={()=>setSeq(s.seq)}>
        镜头 {s.seq} {s.confirmed?'✓':accessibleShot(state.shots,s.seq)?'待确认':'🔒'}</button>)}</div>
      {shot && <article className="guided-card"><h4>镜头 {seq} · {shot.duration} 秒 {shot.confirmed?'· 已确认':'· 待确认'}</h4>
        {state.character_stage_enabled&&state.storyboard_reconfirmed&&<CastReview shot={shot} roles={castState?.characters||[]} state={state} root={root} call={call} run={run} refresh={refresh}/>}
        <label>镜头描述<textarea rows="3" value={prompt} onChange={e=>{setPrompt(e.target.value);setShotPreview(null);}} /></label>
        <button disabled={busy || !prompt.trim() || prompt===shot.prompt} onClick={()=>run(async()=>{
          await call(root+`/shots/${seq}`,'PATCH',{prompt,revision:state.revision});await refresh();onPlanned?.();})}>保存描述</button>
        <p>更改描述或选择其他素材会使本镜头待重新确认，其他已确认镜头不受影响。</p>
        {state.character_stage_enabled&&state.storyboard_reconfirmed&&<GuidedFirstFrames pid={pid} shot={shot} state={state} call={call} run={run} refresh={refresh}/>}
        {state.character_stage_enabled&&!state.storyboard_reconfirmed&&<p className="guided-warning">请先确认所有角色和每个镜头的角色关联。</p>}
        <label>素材版本<select value={shot.asset_id || ''} onChange={e=>run(async()=>{
          await call(`/projects/${encodeURIComponent(pid)}/shots/${seq}/selection`,'PUT',{asset_id:e.target.value});await refresh();onPlanned?.();})}>
          <option value="">选择素材</option>{shotsAssets.map(a=><option key={a.id} value={a.id}>{a.source} · {a.id}</option>)}</select></label>
        {state.character_stage_enabled&&shot.asset_id&&<p>视频来源：{shot.video_provenance?`首帧 ${shot.video_provenance.frame_id.slice(0,8)} · ${shot.video_provenance.model}`:'未知（历史或外部素材）'}</p>}
        {shot.asset_id && <video controls key={shot.asset_id} src={`/api/projects/${encodeURIComponent(pid)}/media/selected/${seq}?asset=${encodeURIComponent(shot.asset_id)}`} />}
        <div className="fields"><label>本次视频预算 USD<input type="number" min="0" step="0.01" value={videoBudget} onChange={e=>setVideoBudget(e.target.value)} /></label>
          <button disabled={busy || prompt!==shot.prompt || !frameVideoReady(state.character_stage_enabled,state.storyboard_reconfirmed,shot,state.video_model)} onClick={()=>run(async()=>setShotPreview(await call(root+`/shots/${seq}/preview`,'POST',{})))}>Dry Run / 预估</button></div>
        {shotPreview && <><p>{shotPreview.paid?`预估 $${shotPreview.estimated_cost_usd}；不是供应商账单硬上限`:'本地 Mock，不调用付费 API'}</p>
          <button disabled={busy} onClick={()=>run(async()=>{
            if(shotPreview.paid && !window.confirm(`确认付费生成镜头 ${seq}？本次预算 $${videoBudget}；供应商可能收取不同金额。`))return;
            await call(root+`/shots/${seq}/generate`,'POST',{budget_usd:Number(videoBudget),confirm_paid:shotPreview.paid,request_id:crypto.randomUUID()});
            setShotPreview(null);await refresh();onPlanned?.();})}>{shotPreview.paid?'授权并生成':'生成本地 Mock'}</button></>}
        {currentAttempts.map(a=><div key={a.id} className="guided-attempt">任务 {a.id.slice(0,8)} · {a.status} {a.error || ''}
          {['SUBMITTING','SUBMITTED','RUNNING','DOWNLOAD_PENDING'].includes(a.status) && <>
            {!a.remote_id && <input placeholder="供应商后台核实的任务 ID" value={remote} onChange={e=>setRemote(e.target.value)} />}
            <button disabled={busy || (!a.remote_id && !remote.trim())} onClick={()=>run(async()=>{
              if(!a.remote_id && !window.confirm('确认已在供应商后台核实此任务 ID？'))return;
              await call(root+`/attempts/${a.id}/recover`,'POST',{remote_id:a.remote_id?null:remote});setRemote('');await refresh();onPlanned?.();})}>恢复 / 查询任务</button></>}</div>)}
        <button disabled={busy || !shot.asset_id || prompt!==shot.prompt} onClick={()=>run(async()=>{
          const updated=await call(root+`/shots/${seq}/confirm`,'POST',{asset_id:shot.asset_id,revision:state.revision});setState(updated);
          const next=updated.shots.find(s=>!s.confirmed);if(next)setSeq(next.seq);onPlanned?.();})}>确认此镜头并继续</button>
      </article>}
      <h3>④ 渲染</h3><button disabled={busy || !allConfirmed} onClick={()=>run(async()=>setRender(await call(root+'/render','POST',{revision:state.revision})))}>渲染已确认镜头</button>
      {!allConfirmed && <p>所有镜头确认后才可渲染。</p>}{render && <p>渲染：{render.status} {render.error || ''} {render.status==='completed' && <a href={`/api/projects/${encodeURIComponent(pid)}/renders/${render.id}/video`}>打开视频</a>}</p>}
    </>}
    {message && <p role="status" className="guided-warning">{message}</p>}
  </section>;
}
