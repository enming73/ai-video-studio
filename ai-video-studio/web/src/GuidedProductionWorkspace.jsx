import React, {useEffect, useState} from 'react';
import {shotReady, validateDraft, castComplete as isCastComplete, frameVideoReady} from './guidedHelpers.js';
import GuidedCharacters from './GuidedCharacters.jsx';
import GuidedBoundaryFrames from './GuidedBoundaryFrames.jsx';

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
  const [boundaryId, setBoundaryId] = useState(null), [structure, setStructure] = useState(null);
  const [videoProvider, setVideoProvider] = useState('minimax');
  const root = `/projects/${encodeURIComponent(pid)}/guided`;
  const refresh = async (preserveDraft = false) => {
    const [s, p, a, jobs, plans] = await Promise.all([
      call(root + '/'), call(`/projects/${encodeURIComponent(pid)}`), call(`/projects/${encodeURIComponent(pid)}/assets`),
      call(root + '/attempts'), call(root + '/plan/attempts')]);
    setState(s); setProject(p.project); if(!preserveDraft)setDraft(s.draft); setAssets(a); setAttempts(jobs); setPlanAttempts(plans);
    setSeq(old => old && s.shots.some(x => x.seq === old) ? old : (s.shots.find(x => !x.confirmed)?.seq || s.shots[0]?.seq || null));
    setBoundaryId(old => (old && s.boundaries?.some(b => b.id === old)) ? old : null);
    return s;
  };
  useEffect(() => {setState(null); setDraft(null); setSeq(null); setBoundaryId(null); setStructure(null); setPlanPreview(null); setShotPreview(null); setRender(null);
    refresh().catch(e => setMessage(e.message));}, [pid]);
  const shot = state?.shots.find(s => s.seq === seq);
  useEffect(() => {setPrompt(shot?.prompt || ''); setShotPreview(null);}, [pid, seq, shot?.prompt]);
  useEffect(() => {if (state?.video_model) setVideoProvider(state.video_model.provider);}, [state?.video_model?.provider]);
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
  const boundaries = state?.boundaries || [];
  const selectedBoundary = boundaries.find(b => b.id === boundaryId) || null;
  const boundaryById = id => boundaries.find(b => b.id === id);
  const structureTotal = structure?.reduce((n,s) => n + (Number(s.duration) || 0), 0) || 0;
  const structureValid = !!structure?.length && structure.every(s => s.prompt?.trim() && Number.isInteger(s.duration) && s.duration > 0);
  const openStructure = () => setStructure(state.shots.map(s => ({id: s.id, prompt: s.prompt, duration: s.duration, character_ids: s.character_ids || []})));
  const structureRevise = (index, field, value) => setStructure(d => d.map((s,i) => i === index ? {...s,[field]:value} : s));
  const structureRemove = index => setStructure(d => d.filter((_,i) => i !== index));
  const submitStructure = async () => {const result = await call(root + '/structure', 'PUT', {revision: state.revision,
    shots: structure.map(s => ({id: s.id, prompt: s.prompt, duration: s.duration, character_ids: s.character_ids || []}))});
    setStructure(null); setState(result); await refresh(true);};
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
    {state.storyboard_confirmed && <><h3>③ 边界帧与逐镜制作</h3>
      {state.character_stage_enabled && boundaries.length > 0 ? <div className="guided-boundary-strip">
        {boundaries.map((b, i) => <React.Fragment key={b.id}>
          <button className={`boundary${b.confirmed ? ' confirmed' : ''}${boundaryId === b.id ? ' active' : ''}`} title={`边界 B${b.position} · ${b.candidate_count} 个候选`}
            onClick={() => setBoundaryId(b.id)}>B{i}{b.confirmed ? ' ✓' : ''}</button>
          {i < state.shots.length && (() => { const s = state.shots[i]; const ready = shotReady(s, boundaries);
            return <button className={`shot${s.confirmed ? ' confirmed' : ''}${seq === s.seq ? ' active' : ''}`}
              onClick={() => setSeq(s.seq)}>镜头 {s.seq}{s.confirmed ? ' ✓' : ready ? ' · 边界就绪' : ' · 待边界'}</button>; })()}
        </React.Fragment>)}
      </div> : <div className="fields">
        {state.shots.map(s => <button key={s.id} className={seq === s.seq ? 'active' : ''} onClick={() => setSeq(s.seq)}>
          镜头 {s.seq} {s.confirmed ? '✓' : '待确认'}</button>)}</div>}
      {selectedBoundary && <GuidedBoundaryFrames pid={pid} boundary={selectedBoundary} state={state} call={call} run={run} refresh={refresh} onClose={() => setBoundaryId(null)} />}
      {shot && <article className="guided-card"><h4>镜头 {seq} · {shot.duration} 秒 {shot.confirmed?'· 已确认':'· 待确认'}</h4>
        {state.character_stage_enabled && <>
          <div className="guided-shot-boundaries">
            {[['起始', shot.start_boundary_id], ['结束', shot.end_boundary_id]].map(([label, id]) => { const b = boundaryById(id);
              return <span key={label}>{label}边界：{b ? `B${b.position} ${b.confirmed ? '已确认' : '待确认'}` : '—'}
                {b && <button onClick={() => setBoundaryId(b.id)}>编辑</button>}</span>; })}
          </div>
          <p>确认起始与结束边界帧后，本镜头才能生成视频。已有视频会保留并标记为旧版本。</p></>}
        {state.character_stage_enabled&&state.storyboard_reconfirmed&&<CastReview shot={shot} roles={castState?.characters||[]} state={state} root={root} call={call} run={run} refresh={refresh}/>}
        <label>镜头描述<textarea rows="3" value={prompt} onChange={e=>{setPrompt(e.target.value);setShotPreview(null);}} /></label>
        <button disabled={busy || !prompt.trim() || prompt===shot.prompt} onClick={()=>run(async()=>{
          await call(root+`/shots/${seq}`,'PATCH',{prompt,revision:state.revision});await refresh();onPlanned?.();})}>保存描述</button>
        <p>更改描述或选择其他素材会使本镜头待重新确认，其他已确认镜头不受影响。</p>
        {state.character_stage_enabled&&state.storyboard_reconfirmed&&<div className="fields"><label>首帧视频模型<select value={videoProvider} onChange={e=>setVideoProvider(e.target.value)}>
          <option value="minimax">MiniMax-H3 · 图生视频</option><option value="mock">Mock · 本地测试</option><option value="flux" disabled>Flux · 图片输入方式待核实</option>
        </select></label><button onClick={()=>run(async()=>{const vmodel=videoProvider==='mock'?'mock':'MiniMax-H3';
          await call(root+'/video-model','PUT',{provider:videoProvider,model:vmodel,revision:state.revision});await refresh(true);})}>选择视频模型</button></div>}
        {state.video_model&&<p>当前视频模型：{state.video_model.provider} / {state.video_model.model}</p>}
        <label>素材版本<select value={shot.asset_id || ''} onChange={e=>run(async()=>{
          await call(`/projects/${encodeURIComponent(pid)}/shots/${seq}/selection`,'PUT',{asset_id:e.target.value});await refresh();onPlanned?.();})}>
          <option value="">选择素材</option>{shotsAssets.map(a=><option key={a.id} value={a.id}>{a.source} · {a.id}</option>)}</select></label>
        {state.character_stage_enabled&&shot.asset_id&&<p>视频来源：{shot.video_provenance?`首帧 ${shot.video_provenance.frame_id.slice(0,8)} · ${shot.video_provenance.model}`:'未知（历史或外部素材）'}</p>}
        {shot.asset_id && <video controls key={shot.asset_id} src={`/api/projects/${encodeURIComponent(pid)}/media/selected/${seq}?asset=${encodeURIComponent(shot.asset_id)}`} />}
        <div className="fields"><label>本次视频预算 USD<input type="number" min="0" step="0.01" value={videoBudget} onChange={e=>setVideoBudget(e.target.value)} /></label>
          <button disabled={busy || prompt!==shot.prompt || !frameVideoReady(state.character_stage_enabled,state.storyboard_reconfirmed,shot,state.video_model,boundaries)} onClick={()=>run(async()=>setShotPreview(await call(root+`/shots/${seq}/preview`,'POST',{})))}>Dry Run / 预估</button></div>
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
      <h3>调整分镜结构</h3>
      {structure ? <>
        <p>删除或修改镜头不会删除已生成的视频文件，仅归档镜头并将受影响视频标记为旧版本。</p>
        {structure.map((s,i) => { const existing = state.shots.find(x => x.id === s.id); const hasAsset = !!existing?.asset_id;
          return <article className="guided-card" key={s.id || `new-${i}`}><h4>镜头 {i+1}{s.id ? '' : '（新增）'}{hasAsset ? ' · 已有素材' : ''}</h4>
            <label>镜头描述<textarea rows="2" value={s.prompt} onChange={e=>structureRevise(i,'prompt',e.target.value)} /></label>
            <div className="fields"><label>时长（秒）<input type="number" min="1" value={s.duration} disabled={hasAsset} onChange={e=>structureRevise(i,'duration',Number(e.target.value))} />
              {hasAsset && <span className="muted">归档并重建</span>}</label>
              <button onClick={()=>structureRemove(i)}>删除镜头</button></div>
            {state.character_stage_enabled && <fieldset><legend>出场角色</legend>
              {(castState?.characters||[]).map(c=><label className="guided-cast-option" key={c.id}><input type="checkbox" checked={(s.character_ids||[]).includes(c.id)} onChange={e=>{
                const ids=s.character_ids||[];structureRevise(i,'character_ids',e.target.checked?[...ids,c.id]:ids.filter(id=>id!==c.id));}}/>{c.name}</label>)}</fieldset>}
          </article>; })}
        <p>总时长 {structureTotal} / {project?.duration} 秒{structureTotal !== project?.duration ? ' · 需与项目时长一致' : ''}</p>
        <button disabled={busy} onClick={()=>setStructure(d=>[...d,{id:null,prompt:'',duration:1,character_ids:[]}])}>＋ 添加镜头</button>{' '}
        <button disabled={busy || !structureValid} onClick={()=>run(submitStructure)}>提交结构调整</button>{' '}
        <button disabled={busy} onClick={()=>setStructure(null)}>取消</button>
      </> : <p><button onClick={openStructure}>调整分镜结构（增删镜头 / 修改时长）</button></p>}
      <h3>④ 渲染</h3><button disabled={busy || !allConfirmed} onClick={()=>run(async()=>setRender(await call(root+'/render','POST',{revision:state.revision})))}>渲染已确认镜头</button>
      {!allConfirmed && <p>所有镜头确认后才可渲染。</p>}{render && <p>渲染：{render.status} {render.error || ''} {render.status==='completed' && <a href={`/api/projects/${encodeURIComponent(pid)}/renders/${render.id}/video`}>打开视频</a>}</p>}
    </>}
    {message && <p role="status" className="guided-warning">{message}</p>}
  </section>;
}
