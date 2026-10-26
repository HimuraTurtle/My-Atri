from __future__ import annotations

PART = r"""  return ({memory:'记忆',insights:'群聊总结',modality:'视觉/语音权限',config:'配置'})[source] || source || '未知来源';
}
function userDirectoryCapabilityLabel(capability) {
  return ({vision:'视觉模型',voice:'语音模型',memory:'记忆'})[capability] || capability;
}
function userDirectoryTimeText(value) {
  if(value === null || value === undefined || value === '') return '暂无';
  const raw = Number(value);
  if(Number.isFinite(raw) && raw > 0) {
    const date = new Date((raw > 100000000000 ? raw : raw * 1000));
    if(!Number.isNaN(date.getTime())) return date.toLocaleString();
  }
  return String(value).replace('T',' ').slice(0,19) || '暂无';
}
function userDirectoryBadge(text, kind='') {
  return `<span class="badge ${kind ? escapeHtml(kind) : ''}">${escapeHtml(text)}</span>`;
}
function userDirectoryEntityKey(item) { return item.entity_type === 'group' ? `group:${item.group_id}` : String(item.user_id || ''); }
function userDirectoryMemory(item) {
  const userId = String(item?.user_id || '');
  return (window._memoryItems || []).find(row=>String(row.target?.user_id || '')===userId)
    || (window._memoryItems || []).find(row=>String(row.id || '')===`person:${userId}` || String(row.id || '')===`private:${userId}`)
    || null;
}
function userDirectoryMemoryCardMarkup(item) {
  const memory=userDirectoryMemory(item);
  const relation=item?.relationship || {};
  // The directory endpoint already carries the compact relationship read
  // model.  Render it immediately, then replace the placeholder with the
  // richer memory record when the lazy memory request finishes.
  if(!memory && !Object.keys(relation).length) return '';
  const data=memory || {...relation, messages:item?.message_count || 0};
  const plan=memory?.proactive_plan || {};
  const nextType=proactiveWeightLabels[plan.next_event_type] || plan.next_event_type || (data.proactive_override ? '按计划' : '暂无');
  const nextAt=plan.next_at ? formatProactiveTime(plan.next_at, ((window._proactive||{}).schedule||{}).timezone) : '暂无';
  const counts=data.memory_counts || {};
  const summary=data.summary || (memory ? '暂无可读摘要' : '记忆摘要加载中…');
  const proactive=plan.eligible===false?'未入选':(plan.eligible?'已入选':(data.proactive_override ? '按计划' : '暂无计划'));
  return `<span class="user-directory-inline-memory"><strong>记忆关系与上下文</strong><span class="card-context-values"><span>好感 ${formatScore(data.affection)}</span><span>信任 ${escapeHtml(data.trust_label || data.trust_tier || '观察中')}</span><span>记忆 ${memoryCountText(counts)}</span><span>消息 ${Number(data.messages||item?.message_count||0)}</span></span><span class="card-context-summary">${escapeHtml(summary)}</span><span class="memory-meta">主动互动：${escapeHtml(proactive)} · 下次内容：${escapeHtml(nextType)} · ${escapeHtml(nextAt)}</span></span>`;
}
function directoryWithTimeout(promise, timeoutMs=2200) {
  return Promise.race([promise,new Promise((_,reject)=>setTimeout(()=>reject(new Error('timeout')),timeoutMs))]);
}
async function loadDirectorySchedules() {
  const schedules=await directoryWithTimeout(api('/api/group-summaries/schedules'),1800).catch(()=>({schedules:[]}));
  window._summarySchedules=schedules.schedules||window._summarySchedules||[];
  renderUserDirectory();
}
function renderDirectoryProactiveState() {
  const state=window._proactive||{}; const policy=state.policy||{}; const enabled=state.feature_enabled!==false && policy.enabled!==false;
  if($('directoryProactiveEnabled')) $('directoryProactiveEnabled').checked=enabled;
  if($('directoryProactiveState')) $('directoryProactiveState').textContent=state.feature_enabled===false?'新版主动消息已关闭':(enabled?'主动消息运行中':'主动消息已暂停');
}
async function saveDirectoryProactiveToggle() {
  const enabled=Boolean($('directoryProactiveEnabled')?.checked); const current=JSON.parse(JSON.stringify(window._proactive?.policy||{})); current.enabled=enabled;
  try { await api('/api/config',{method:'POST',body:JSON.stringify({PROACTIVE_V2_ENABLED:enabled})}); await api('/api/proactive/save',{method:'POST',body:JSON.stringify(current)}); window._proactive={...(window._proactive||{}),policy:current,feature_enabled:enabled}; renderDirectoryProactiveState(); toast(enabled?'主动消息已启用':'主动消息已暂停'); }
  catch(error) { toast('主动消息设置保存失败：'+error.message); renderDirectoryProactiveState(); }
}
function directorySummaryControlId(groupId, kind, suffix) {
  return ('directory_summary_' + kind + '_' + groupId + '_' + suffix).replace(/[^a-zA-Z0-9_-]/g,'_');
}
function userDirectoryGroupSummaryCompactMarkup(item) {
  const row=(window._summarySchedules||[]).find(value=>String(value.group_id)===String(item.group_id));
  const relation=item?.relationship||{};
  const counts=relation.memory_counts||{};
  const activity=relation.activity;
  const activityText=(activity===null||activity===undefined||activity==='')?'暂无':formatScore(activity);
  const summary=relation.summary||'暂无可读群聊摘要';
  const state=(key,label)=>`${label} ${Number(row[key+'_enabled']||0)?'已启用':'未启用'}`;
  const next=row ? [row.daily_next_at&&`日报 ${String(row.daily_next_at).replace('T',' ')}`,row.weekly_next_at&&`周报 ${String(row.weekly_next_at).replace('T',' ')}`,row.monthly_next_at&&`月报 ${String(row.monthly_next_at).replace('T',' ')}`].filter(Boolean).join(' · ') || '暂无下次计划' : '总结计划读取中…';
  const plans=row ? `<span class="card-context-values group-summary-plan-values"><span>${state('daily','日报')}</span><span>${state('weekly','周报')}</span><span>${state('monthly','月报')}</span></span>` : '';
  return `<span class="user-directory-inline-memory group-directory-memory"><strong>群聊状态与记忆</strong><span class="card-context-values"><span>活跃度 ${escapeHtml(activityText)} · ${escapeHtml(relation.activity_label||'暂无评级')}</span><span>消息 ${Number(item.message_count||0)}</span><span>记忆 ${escapeHtml(memoryCountText(counts))}</span><span>最近 ${escapeHtml(userDirectoryTimeText(item.last_seen_at))}</span></span><span class="card-context-summary">${escapeHtml(summary)}</span>${plans}<span class="memory-meta">${escapeHtml(next)}</span></span>`;
}
function userDirectoryGroupSummaryMarkup(item) {
  const row=(window._summarySchedules||[]).find(value=>String(value.group_id)===String(item.group_id));
  if(!row) return `<div class="user-directory-context user-directory-group-summary"><h4>群聊总结</h4><p class="note">尚未读取到该群的总结计划，刷新用户中心后会同步。</p></div>`;
  const plan=(kind,label)=>{ const enabled=Boolean(Number(row[kind+'_enabled']||0)); const next=row[kind+'_next_at']||''; return `<div class="summary-plan"><label class="switch-line"><input id="${directorySummaryControlId(item.group_id,kind,'enabled')}" type="checkbox" ${enabled?'checked':''}>${label}</label><input id="${directorySummaryControlId(item.group_id,kind,'next_at')}" type="datetime-local" value="${escapeHtml(next)}"><span class="summary-last-sent">${row[kind+'_last_sent_at']?'上次发送：'+escapeHtml(String(row[kind+'_last_sent_at']).replace('T',' ')):'尚未自动发送'}</span><button class="ghost mini" onclick="generateGroupSummary('${escapeHtml(item.group_id)}','${kind}',true)">立即生成并发送</button></div>`; };
  return `<div class="user-directory-context summary-context user-directory-group-summary"><div class="section-head"><div><h4>群聊总结计划</h4><p class="note">日报、周报、月报均在此卡片中设置。</p></div><div class="row"><button class="ghost" onclick="saveDirectoryGroupSummary(this,'${escapeHtml(item.group_id)}')">保存本群计划</button><button class="ghost" onclick="loadDirectoryGroupSummaries(this,'${escapeHtml(item.group_id)}')">加载近期总结</button></div></div><div class="summary-plan-grid">${plan('daily','日报')}${plan('weekly','周报')}${plan('monthly','月报')}</div><div class="directory-group-summary-history"></div></div>`;
}
async function loadDirectoryGroupSummaries(button, groupId) {
  const panel=button?.closest('.user-directory-group-summary')?.querySelector('.directory-group-summary-history');
  if(!panel) return;
  panel.innerHTML='<p class="note">正在读取近期总结…</p>';
  try {
    const data=await api('/api/group-summaries?group_id='+encodeURIComponent(groupId)+'&limit=20'); const items=data.items||[];
    panel.innerHTML=items.length ? items.map(item=>`<article class="summary-detail-item"><div class="entry-head"><strong>${escapeHtml(item.title||'群聊总结')}</strong><span class="badge ${item.status==='completed'?'active':(item.status==='failed'?'red':'warn')}" >${escapeHtml(item.status||'')}</span></div><p class="note">${escapeHtml(item.period_label||'')} · ${escapeHtml(String(item.created_at||'').replace('T',' '))} · ${Number(item.message_count||0)} 条消息</p><div class="summary-detail-content">${escapeHtml(item.content||item.error||'报告正在生成…')}</div></article>`).join('') : '<p class="note">本群暂无已生成总结。</p>';
  } catch(error) { panel.innerHTML='<p class="bad">读取群聊总结失败：'+escapeHtml(error.message)+'</p>'; }
}
async function saveDirectoryGroupSummary(button, groupId) {
  if(typeof button === 'string') { groupId=button; button=null; }
  return withButtonBusy(button, ()=>saveDirectoryGroupSummaryCore(groupId), '保存中…');
}
async function saveDirectoryGroupSummaryCore(groupId) {
  const plan=kind=>({enabled:Boolean($(directorySummaryControlId(groupId,kind,'enabled'))?.checked),next_at:$(directorySummaryControlId(groupId,kind,'next_at'))?.value||''});
  try { await api('/api/group-summaries/schedules',{method:'POST',body:JSON.stringify({group_id:String(groupId),daily:plan('daily'),weekly:plan('weekly'),monthly:plan('monthly')})}); toast('群聊总结计划已保存'); const data=await api('/api/group-summaries/schedules'); window._summarySchedules=data.schedules||[]; renderUserDirectory(); }
  catch(error) { toast('群聊总结计划保存失败：'+error.message); }
}
function userDirectoryDetailMarkup(item) {
  if(item.entity_type === 'group') {
    return `<div class="user-directory-inline-detail">${userDirectoryGroupSummaryMarkup(item)}</div>`;
  }
  const roles = item.roles || [];
  const modalityPolicy = window._modalityAccess?.policy?.users?.[String(item.user_id)] || {};
  const memory = userDirectoryMemory(item);
  const modalityMarkup = roles.includes('bot') ? '' : `<div class="row"><label class="switch-line"><input class="user-directory-vision" type="checkbox" ${modalityPolicy.vision?'checked':''}>视觉模型</label><label class="switch-line"><input class="user-directory-voice" type="checkbox" ${modalityPolicy.voice?'checked':''}>语音模型</label><button class="ghost" onclick="saveUserDirectoryModality(this,'user:${escapeHtml(item.user_id)}')">保存权限</button></div>`;
  const memoryAction = memory ? `<div class="row"><button class="ghost" onclick="openMemory('${escapeHtml(memory.id)}')">编辑记忆</button></div>` : '';
  return `<div class="user-directory-inline-detail">${memoryAction}${modalityMarkup}</div>`;
}
function renderUserDirectory() {
  const state = window._userDirectory || {items:[],groups:[]};
  const query = String($('userDirectorySearch')?.value || '').trim().toLowerCase();
  const source = String($('userDirectorySource')?.value || '');
  const entityType = String($('userDirectoryEntityType')?.value || 'all');
  const matches = item => {
    const haystack = [item.user_id,item.group_id,item.canonical_name,...(item.aliases||[]),...(item.sources||[]),...(item.contexts||[])].join(' ').toLowerCase();
    return (!query || haystack.includes(query)) && (!source || (item.sources||[]).includes(source));
  };
  const users = entityType === 'group' ? [] : (state.items || []).filter(matches);
  const groups = entityType === 'user' ? [] : (state.groups || []).filter(matches);
  // 保留亚托莉与主人两个置顶卡片的原有顺序，其余用户按好感度降序排列。
  // 好感度来自统一用户目录的 relationship，兼容旧接口直接返回 affection 的情况。
  const affectionOf = item => {
    const relationship = item.relationship || {};
    const value = relationship.affection ?? relationship.affection_score ?? item.affection ?? item.affection_score;
    const score = Number(value);
    return Number.isFinite(score) ? score : -Infinity;
  };
  const sortedUsers = users.map((item, index) => ({item, index})).sort((left, right) => {
    const a = left.item;
    const b = right.item;
    const aPin = Number.isFinite(Number(a.pin_order)) ? Number(a.pin_order) : (a.roles?.includes('bot') ? 0 : (a.roles?.includes('owner') ? 1 : 2));
    const bPin = Number.isFinite(Number(b.pin_order)) ? Number(b.pin_order) : (b.roles?.includes('bot') ? 0 : (b.roles?.includes('owner') ? 1 : 2));
    if(aPin < 2 || bPin < 2) return aPin - bPin || left.index - right.index;
    return affectionOf(b) - affectionOf(a)
      || Number(b.message_count || 0) - Number(a.message_count || 0)
      || left.index - right.index;
  }).map(entry => entry.item);
  const card = item => {
    const key = userDirectoryEntityKey(item); const selected = (state.selectedIds || []).includes(key);
    const isGroup = item.entity_type === 'group'; const roles = item.roles || [];
    const roleLabel = roles.includes('bot') ? '机器人' : (roles.includes('owner') ? '所有者' : '');
    const title = isGroup ? `群聊 · ${item.canonical_name}` : item.canonical_name;
    const rowClass = `${isGroup?'group-row':''} ${roles.includes('bot')?'bot-row':''} ${roles.includes('owner')?'owner-row':''}`;
    // 亚托莉不是普通用户档案：主卡只显示运行身份、消息和最近活动。
    const cardContext = isGroup ? userDirectoryGroupSummaryCompactMarkup(item) : (roles.includes('bot') ? '' : userDirectoryMemoryCardMarkup(item));
    const activity=item.last_seen_at || '';
    return `<div class="user-directory-entry"><button type="button" class="user-directory-row ${selected?'selected':''} ${rowClass}" onclick="selectUserDirectoryUser('${escapeHtml(key)}')"><strong>${escapeHtml(title)} ${roleLabel?userDirectoryBadge(roleLabel,'active'):''}</strong><span class="memory-meta">${isGroup?'群号 '+escapeHtml(item.group_id):'QQ '+escapeHtml(item.user_id)} · ${Number(item.message_count||0)} 条消息${activity ? ' · 最近 '+escapeHtml(userDirectoryTimeText(activity)) : ''}</span>${cardContext}</button>${selected ? userDirectoryDetailMarkup(item) : ''}</div>`;
  };
  const target = $('userDirectoryRows');
  if(!target) return;
  const html = [sortedUsers.length ? `<div class="user-directory-section-title">用户（${sortedUsers.length}）</div>${sortedUsers.map(card).join('')}` : '', groups.length ? `<div class="user-directory-section-title">群聊（${groups.length}）</div>${groups.map(card).join('')}` : ''].join('');
  target.innerHTML = html || '<div class="empty">没有匹配的用户或群聊。</div>';
  const status = state.sourceStatus || {};
  if($('userDirectoryStatus')) $('userDirectoryStatus').textContent = `用户 ${users.length}/${Number(state.total || state.items?.length || 0)} · 群聊 ${groups.length}/${Number(state.groupTotal || state.groups?.length || 0)} · 记忆 ${status.memory || '—'} · 摘要 ${status.insights || '—'}`;
}
function userDirectoryOpenTab(id) {
  const button = Array.from(document.querySelectorAll('.tab')).find(item => item.getAttribute('onclick') === `showTab(event,'${id}')`);
  if(button) button.click();
}
async function selectUserDirectoryUser(userId, forceOpen=false) {
  const state = window._userDirectory || {};
  const key = String(userId || '');
  state.selectedIds = Array.isArray(state.selectedIds) ? state.selectedIds : [];
  const index = state.selectedIds.indexOf(key);
  if(forceOpen) { if(index < 0) state.selectedIds.push(key); }
  else if(index >= 0) state.selectedIds.splice(index, 1);
  else state.selectedIds.push(key);
  renderUserDirectory();
}
async function saveUserDirectoryModality(button, entityKey) {
  return withButtonBusy(button, ()=>saveUserDirectoryModalityCore(button, entityKey), '保存中…');
}
async function saveUserDirectoryModalityCore(button, entityKey) {
  const key = String(entityKey || '').trim();
  const isGroup = key.startsWith('group:');
  const id = key.replace(/^(user:|group:)/, '');
  if(!/^\d+$/.test(id)) return toast('对象编号无效');
  try {
    const data = window._modalityAccess || await api('/api/modality-access');
    const policy = JSON.parse(JSON.stringify(data.policy || {version:1,vision_allowlist_enabled:false,voice_allowlist_enabled:false,users:{}}));
    const container = button?.closest('.user-directory-inline-detail');
    const vision = Boolean(container?.querySelector('.user-directory-vision')?.checked);
    const voice = Boolean(container?.querySelector('.user-directory-voice')?.checked);
    if(isGroup) {
      policy.groups = policy.groups || {};
      const current = policy.groups[id] || {};
      const item = (window._userDirectory?.groups || []).find(row=>String(row.group_id)===id);
      policy.groups[id] = {name:String(current.name || item?.canonical_name || id), vision, voice};
    } else {
      policy.users = policy.users || {};
      const current = policy.users[id] || {};
      const item = (window._userDirectory?.items || []).find(row=>String(row.user_id)===id);
      policy.users[id] = {name:String(current.name || item?.canonical_name || id), vision, voice};
    }
    const result = await api('/api/modality-access',{method:'POST',body:JSON.stringify(policy)});
    window._modalityAccess = {...data, policy:result.policy};
    await loadUserDirectory(false);
    await selectUserDirectoryUser(`${isGroup?'group':'user'}:${id}`, true);
    toast(`${isGroup?'群聊':'用户'}视觉 / 语音权限已保存`);
  } catch(error) { toast('权限保存失败：'+error.message); }
}
async function loadUserDirectory() {
  const target = $('userDirectoryRows');
  if(target && !window._userDirectory?.items?.length) {
    // 上一次成功读取的核心目录先用于首屏，网络慢时不再让整个用户中心空白。
    try {
      const cached=JSON.parse(localStorage.getItem('atri.userDirectory.core') || 'null');
      if(cached?.items?.length || cached?.groups?.length) {
        window._userDirectory={...cached, selectedIds:[]};
        renderUserDirectory();
      } else target.innerHTML = '<div class="empty">正在读取...</div>';
    } catch(error) { target.innerHTML = '<div class="empty">正在读取...</div>'; }
  }
  try {
    const data = await api('/api/users?limit=1000');
    const sourceStatus={...(data.source_status||{})};
    window._userDirectory = {items:data.items || [], groups:data.groups || [], total:Number(data.total||0), groupTotal:Number(data.group_total||data.groups?.length||0), selectedIds:Array.isArray(window._userDirectory?.selectedIds) ? window._userDirectory.selectedIds : [], sourceStatus};
    try { localStorage.setItem('atri.userDirectory.core', JSON.stringify(window._userDirectory)); } catch(error) { /* private mode/storage quota */ }
    await loadDirectorySchedules();
    renderUserDirectory();
  } catch(error) {
    if($('userDirectoryStatus')) $('userDirectoryStatus').textContent = '用户目录读取失败：' + error.message;
    if(target) target.innerHTML = `<div class="empty">读取失败：${escapeHtml(error.message)}</div>`;
  }
}
async function runUserDirectoryRefresh(button) {
  await withButtonBusy(button, async()=>{ await loadUserDirectory(); toast('用户目录已刷新'); }, '刷新中…');
}
async function openUserInDirectory(userId) {
"""
