export function validateDraft(draft, total, allowed) {
  if (!draft?.shots?.length) return '请至少添加一个镜头';
  if (draft.shots.some(s => !s.prompt?.trim() || !Number.isInteger(s.duration) || s.duration <= 0)) return '请填写每个镜头的描述和正整数时长';
  if (allowed?.length && draft.shots.some(s => !allowed.includes(s.duration))) return '镜头时长不符合当前 Provider 的约束';
  if (draft.shots.reduce((sum, s) => sum + s.duration, 0) !== total) return '镜头总时长必须与项目时长一致';
  return '';
}

export function accessibleShot(shots, seq) {
  const shot = shots.find(s => s.seq === seq);
  return !!shot && (shot.confirmed || shots.filter(s => s.seq < seq).every(s => s.confirmed));
}

export function castComplete(enabled, cast, draft) {
  if (!enabled) return true;
  if (!cast || !draft?.shots?.length) return false;
  const rolesConfirmed = cast.characters.length ? cast.characters.every(c=>c.confirmed) : cast.empty_cast_confirmed;
  return !!rolesConfirmed && draft.shots.every(s=>Array.isArray(s.character_ids) && s.character_ids.every(id=>cast.characters.some(c=>c.id===id)));
}

export function frameVideoReady(enabled, storyboardConfirmed, shot, videoModel) {
  return !enabled || !!(storyboardConfirmed && shot?.confirmed_frame_id && videoModel);
}
