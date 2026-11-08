import React, {useEffect, useState} from 'react';

export default function GuidedBoundaryFrames({pid, boundary, state, call, run, refresh, onClose}) {
  const root = `/projects/${encodeURIComponent(pid)}/guided`;
  const [frames, setFrames] = useState([]);
  const [budget, setBudget] = useState('1');
  const [preview, setPreview] = useState(null);
  const [dims, setDims] = useState({});
  useEffect(() => {
    setPreview(null);
    setDims({});
    if (boundary) call(root + `/boundaries/${boundary.id}/frames`).then(setFrames).catch(() => setFrames([]));
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
    {preview && <><p>将发送边界提示词及 {preview.references.length} 张已确认的角色参考图；价格未知，预算不是供应商硬上限。</p>
      <button disabled={Number(budget) <= 0} onClick={() => run(async () => {
        if (!window.confirm(`确认付费生成边界 B${boundary.position}？预算 $${budget}，实际账单可能不同。`)) return;
        const r = await call(root + `/boundaries/${boundary.id}/frames`, 'POST', {model: 'gpt-image-2.5-sunburst', budget_usd: Number(budget), confirm_paid: true, request_id: crypto.randomUUID(), revision: state.revision});
        setPreview(null); await refresh(true);
        if (r.status !== 'COMPLETED') throw Error('边界帧请求结果不明，请先核查供应商账单。');
      })}>授权生成边界帧</button></>}
  </section>;
}
