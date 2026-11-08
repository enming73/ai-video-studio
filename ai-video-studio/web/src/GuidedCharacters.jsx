import React, {useEffect, useState} from 'react';

function CharacterCard({pid, role, state, call, run, refresh}) {
  const root = `/projects/${encodeURIComponent(pid)}/guided`;
  const [name,setName]=useState(role.name), [description,setDescription]=useState(role.description);
  const [pictures,setPictures]=useState([]), [file,setFile]=useState(null), [budget,setBudget]=useState('1');
  const [preview,setPreview]=useState(null);
  useEffect(()=>{setName(role.name);setDescription(role.description);},[role.name,role.description]);
  useEffect(()=>{call(root+`/characters/${role.id}/images`).then(setPictures).catch(()=>setPictures([]));},[pid,role.id,state.revision]);
  return <article className="guided-card character-card"><div className="guided-card-head"><h4>{role.name}</h4>
    <span>{role.confirmed?'已确认':'待确认'}</span></div>
    <label>角色名<input value={name} onChange={e=>{setName(e.target.value);setPreview(null);}}/></label>
    <label>外观与角色设定<textarea rows="3" value={description} onChange={e=>{setDescription(e.target.value);setPreview(null);}}/></label>
    <button disabled={!name.trim()||!description.trim()||(name===role.name&&description===role.description)} onClick={()=>run(async()=>{
      await call(root+`/characters/${role.id}`,'PATCH',{name,description,revision:state.revision});await refresh(true);})}>保存角色设定</button>
    <div className="guided-image-grid">{pictures.map(img=><figure key={img.id} className={role.selected_image_id===img.id?'selected':''}>
      <img src={`/api${root}/images/${img.id}`} alt={`${role.name} 定妆候选`} /><figcaption>{img.source==='upload'?'上传':'生成'} · {img.id.slice(0,6)}</figcaption>
      <button onClick={()=>run(async()=>{await call(root+`/characters/${role.id}/select`,'POST',{image_id:img.id,revision:state.revision});await refresh(true);})}>选择此图</button>
    </figure>)}</div>
    <div className="fields"><label>上传定妆照<input type="file" accept="image/png,image/jpeg,image/webp" onChange={e=>setFile(e.target.files?.[0] || null)}/></label>
      <button disabled={!file} onClick={()=>run(async()=>{const form=new FormData();form.append('revision',state.revision);form.append('file',file);
        const r=await fetch(`/api${root}/characters/${role.id}/images`,{method:'POST',body:form});const data=await r.json();if(!r.ok)throw Error(data.detail||'上传失败');setFile(null);await refresh(true);})}>上传为新版本</button></div>
    <div className="fields"><label>本次定妆预算 USD<input type="number" min="0" step="0.01" value={budget} onChange={e=>setBudget(e.target.value)}/></label>
      <button onClick={()=>run(async()=>setPreview(await call(root+`/characters/${role.id}/images/preview`,'POST',{revision:state.revision})))}>Dry Run / 定妆预览</button></div>
    {preview&&<><p>发送至图片服务：{preview.prompt}；价格未知，预算不是供应商硬上限。</p><button disabled={Number(budget)<=0} onClick={()=>run(async()=>{
      if(!window.confirm(`确认付费生成 ${role.name} 定妆照？预算 $${budget}，实际账单可能不同。`))return;
      const r=await call(root+`/characters/${role.id}/images/generate`,'POST',{revision:state.revision,budget_usd:Number(budget),confirm_paid:true,request_id:crypto.randomUUID()});
      setPreview(null);await refresh(true);if(r.status!=='COMPLETED')throw Error('图片请求结果不明，请先核查供应商账单。');})}>授权生成定妆照</button></>}
    <button disabled={!role.selected_image_id||role.confirmed} onClick={()=>run(async()=>{
      await call(root+`/characters/${role.id}/confirm`,'POST',{image_id:role.selected_image_id,revision:state.revision});await refresh(true);})}>确认该角色</button>
  </article>;
}

export default function GuidedCharacters({pid,state,call,run,refresh,onRoles}) {
  const root=`/projects/${encodeURIComponent(pid)}/guided`;
  const [data,setData]=useState(null),[name,setName]=useState(''),[description,setDescription]=useState(''),[attempts,setAttempts]=useState([]);
  useEffect(()=>{if(!state.character_stage_enabled){setData(null);onRoles(null);return;}
    Promise.all([call(root+'/characters'),call(root+'/image-attempts')]).then(([value,jobs])=>{setData(value);setAttempts(jobs);onRoles(value);}).catch(()=>{});
  },[pid,state.revision,state.character_stage_enabled]);
  if(!state.character_stage_enabled)return <section className="guided-card"><h3>角色定妆（可选启用）</h3>
    <p>为此已有项目启用角色与首帧阶段；旧素材保留，启用后需要重新确认角色及镜头关联。</p>
    <button onClick={()=>run(async()=>{if(!window.confirm('为当前项目启用角色定妆和首帧流程？'))return;
      await call(root+'/characters/activate','POST',{revision:state.revision});await refresh(true);})}>启用角色定妆</button></section>;
  if(!data)return <p>正在加载角色…</p>;
  return <section className="guided-characters"><h3>角色定妆 · {data.characters.filter(c=>c.confirmed).length} / {data.characters.length} 已确认</h3>
    <p>上传或生成角色参考图，选择一张主参考并确认。更换定妆不会删除历史版本。</p>
    {!data.characters.length&&<><button onClick={()=>run(async()=>{await call(root+'/characters/import-existing','POST',{revision:state.revision});await refresh(true);})}>导入已有角色建议（免费）</button>{' '}
      <button disabled={data.empty_cast_confirmed} onClick={()=>run(async()=>{await call(root+'/characters/empty-confirm','POST',{revision:state.revision});await refresh(true);})}>{data.empty_cast_confirmed?'已确认无角色':'确认故事没有角色'}</button></>}
    {data.characters.map(role=><CharacterCard key={role.id} pid={pid} role={role} state={state} call={call} run={run} refresh={refresh}/>)}
    <div className="guided-card"><h4>添加角色</h4><label>角色名<input value={name} onChange={e=>setName(e.target.value)}/></label>
      <label>角色设定<textarea rows="2" value={description} onChange={e=>setDescription(e.target.value)}/></label>
      <button disabled={!name.trim()||!description.trim()} onClick={()=>run(async()=>{await call(root+'/characters','POST',{name,description,revision:state.revision});setName('');setDescription('');await refresh(true);})}>添加角色</button></div>
    {attempts.filter(a=>['SUBMITTING','UNKNOWN'].includes(a.status)).map(a=><div className="guided-warning" key={a.id}>
      图片请求 {a.id.slice(0,8)} 结果不明，可能已计费；请先核查供应商账单。
      <button onClick={()=>run(async()=>{if(!window.confirm('确认已核查供应商账单，并允许重新授权？'))return;
        await call(root+`/image-attempts/${a.id}/resolve`,'POST',{confirm_checked:true});await refresh(true);})}>已核查，解除阻止</button></div>)}
  </section>;
}
