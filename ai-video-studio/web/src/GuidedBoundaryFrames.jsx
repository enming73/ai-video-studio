import React, {useEffect, useState} from 'react';

export default function GuidedBoundaryFrames({pid, boundary, state, call, run, refresh, busy, onClose}) {
  const root = `/projects/${encodeURIComponent(pid)}/guided`;
  const [frames, setFrames] = useState([]);
  const [budget, setBudget] = useState('1');
  const [preview, setPreview] = useState(null);
  const [dims, setDims] = useState({});
  const [promptInfo, setPromptInfo] = useState(null);
  const [promptModel, setPromptModel] = useState('gpt-4o-mini');
  const [promptBudget, setPromptBudget] = useState('0.1');
  const [promptAttempts, setPromptAttempts] = useState([]);
  const [promptError, setPromptError] = useState('');
  useEffect(() => {
    setPreview(null);
    setDims({});
    setPromptError('');
    if (!boundary) return;
    call(root + `/boundaries/${boundary.id}/frames`).then(setFrames).catch(() => setFrames([]));
    call(root + `/boundaries/${boundary.id}/prompt`).then(setPromptInfo).catch(() => setPromptInfo(null));
    call(root + '/prompt-attempts')
      .then(rows => setPromptAttempts(rows.filter(a => a.boundary_id === boundary.id)))
      .catch(() => setPromptAttempts([]));
  }, [pid, boundary?.id, state.revision]);

  if (!boundary) return null;
  const shotsCount = state.shots.length;
  const affectedSeqs = [boundary.position, boundary.position + 1].filter(seq => seq >= 1 && seq <= shotsCount);
  const affectedNames = affectedSeqs.map(seq => `镜头 ${seq}`).join('、');
  const is16x9 = f => { const d = dims[f.id]; if (!d || !d.w || !d.h) return false; return Math.abs(d.w / d.h - 16 / 9) <= 0.01; };

  return <section className="guided-card guided-boundary-editor">
    <div className="guided-card-head"><h4>边界 B{boundary.position} · {boundary.confirmed ? '已确认' : '待确认'}</h4>
      {onClose && <button onClick={onClose}>关闭</button>}</div>
    <p>此边界连接 {affectedNames || '无镜头'}。确认后相邻镜头可进入视频阶段；已有视频会保留并标记为旧版本（stale），不会被删除。</p>
    {boundary.confirmed && <button onClick={() => run(async () => {
      if (!window.confirm(`取消确认边界 B${boundary.position}？\n${affectedNames} 的已有视频会保留并标记为旧版本，不会删除。`)) return;
      await call(root + `/boundaries/${boundary.id}/confirm`, 'DELETE', {revision: state.revision});
      await refresh(true);
    })}>取消确认</button>}
    <section className="guided-prompt-panel">
      <h5>边界提示词</h5>
      {promptInfo && <>
        <p>当前来源：{promptInfo.effective_source === 'llm'
          ? `AI（${promptInfo.llm_prompt_model}${promptInfo.llm_prompt_current ? '' : ' · 已过期'}）`
          : '自动规则'}</p>
        {!promptInfo.llm_prompt && <p className="guided-warning">尚未生成 AI 提示词，当前使用自动规则。要 AI 版请点下方按钮；生成边界帧本身不会自动调用 LLM。</p>}
        {promptInfo.llm_prompt && <>
          <p>AI 提示词{promptInfo.llm_prompt_current ? '' : '（已过期，当前回退自动规则）'}：</p>
          <pre className="guided-prompt">{promptInfo.llm_prompt}</pre>
        </>}
        <p>实际发送：</p>
        <pre className="guided-prompt">{promptInfo.effective_prompt}</pre>
      </>}
      {promptError && <p className="guided-warning">{promptError}</p>}
      <div className="fields">
        <label>提示词模型<input value={promptModel} onChange={e => setPromptModel(e.target.value)} /></label>
        <label>本次预算 USD<input type="number" min="0" step="0.01" value={promptBudget} onChange={e => setPromptBudget(e.target.value)} /></label>
        <button disabled={busy || !promptModel.trim() || Number(promptBudget) <= 0} onClick={() => run(async () => {
          setPromptError('');
          try {
            if (!window.confirm(`确认将相邻镜头描述发送给 ${promptModel} 生成边界提示词？\n本次预算 $${promptBudget}，不是供应商硬上限。`)) return;
            const r = await call(root + `/boundaries/${boundary.id}/prompt`, 'POST', {
              model: promptModel, budget_usd: Number(promptBudget), confirm_paid: true,
              request_id: crypto.randomUUID(), revision: state.revision});
            await refresh(true);
            if (r.status !== 'COMPLETED') throw Error('提示词请求结果不明，请先核查供应商账单。');
          } catch (e) {
            setPromptError(e.message);
            throw e;
          }
        })}>AI 写这个边界的提示词（付费）</button>
      </div>
      {promptInfo?.llm_prompt && <div className="fields"><button onClick={() => run(async () => {
        if (!window.confirm('恢复自动提示词？AI 提示词记录会删除，已生成的图片不受影响。')) return;
        await call(root + `/boundaries/${boundary.id}/prompt`, 'DELETE', {revision: state.revision});
        await refresh(true);
      })}>恢复自动提示词</button></div>}
      {promptAttempts.filter(a => ['SUBMITTING', 'UNKNOWN'].includes(a.status)).map(a =>
        <div className="guided-warning" key={a.id}>上次提示词请求结果不明，可能已计费：{a.error || a.status}。
          <button onClick={() => run(async () => {
            if (!window.confirm('确认已核查此请求，允许以后重新生成？')) return;
            await call(root + `/prompt-attempts/${a.id}/resolve`, 'POST', {confirm_checked: true});
            await refresh(true);
            const rows = await call(root + '/prompt-attempts');
            setPromptAttempts(rows.filter(x => x.boundary_id === boundary.id));
          })}>已核查，解除阻止</button></div>)}
    </section>
    <div className="guided-image-grid guided-boundary-grid">{frames.map(f => <figure key={f.id} className={f.confirmed ? 'selected' : ''}>
      <img src={`/api${root}/images/${f.id}`} alt={`边界 B${boundary.position} 候选`}
        onLoad={e => setDims(d => ({...d, [f.id]: {w: e.target.naturalWidth, h: e.target.naturalHeight}}))} />
      <figcaption>{f.stale ? '基于旧版本 · ' : ''}{f.confirmed ? '已确认 · ' : ''}{f.id.slice(0, 6)}</figcaption>
      {!f.confirmed && (is16x9(f) ? <button onClick={() => run(async () => {
        if (f.stale && !window.confirm('这张边界帧基于旧版本，确认继续使用？')) return;
        if (!window.confirm(`确认将这张边界帧用于 ${affectedNames}？\n${affectedNames} 已有视频会保留并标记为旧版本，不会删除。`)) return;
        await call(root + `/boundaries/${boundary.id}/frames/${f.id}/confirm`, 'POST', {revision: state.revision, allow_stale: !!f.stale});
        await refresh(true);
      })}>确认此边界帧</button> : <button onClick={() => run(async () => {
        await call(root + `/boundaries/${boundary.id}/frames/${f.id}/crop`, 'POST', {revision: state.revision});
        await refresh(true);
      })}>裁剪为 16:9</button>)}
    </figure>)}</div>
    <div className="fields"><label>本次边界帧预算 USD<input type="number" min="0" step="0.01" value={budget} onChange={e => setBudget(e.target.value)} /></label>
      <button onClick={() => run(async () => setPreview(await call(root + `/boundaries/${boundary.id}/frames/preview`, 'POST', {model: 'gpt-image-2.5-sunburst'})))}>Dry Run / 边界帧预览</button></div>
    {preview && <><p>将发送{preview.prompt_source === 'llm' ? ' AI ' : '自动'}边界提示词及 {preview.references.length} 张已确认的角色参考图；价格未知，预算不是供应商硬上限。</p>
      <details><summary>查看将发送的提示词</summary><pre className="guided-prompt">{preview.prompt}</pre></details>
      <button disabled={Number(budget) <= 0} onClick={() => run(async () => {
        if (!window.confirm(`确认付费生成边界 B${boundary.position}？预算 $${budget}，实际账单可能不同。`)) return;
        const r = await call(root + `/boundaries/${boundary.id}/frames`, 'POST', {model: 'gpt-image-2.5-sunburst', budget_usd: Number(budget), confirm_paid: true, request_id: crypto.randomUUID(), revision: state.revision});
        setPreview(null); await refresh(true);
        if (r.status !== 'COMPLETED') throw Error('边界帧请求结果不明，请先核查供应商账单。');
      })}>授权生成边界帧</button></>}
  </section>;
}
