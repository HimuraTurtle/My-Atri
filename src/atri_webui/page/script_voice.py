from __future__ import annotations

PART = r"""async function loadVoice() {
  const data = await api('/api/music/overview');
  const musicService = data.music_service || {};
  const projects = musicService.projects || {};
  $('musicServiceStatus').innerHTML = [
    ['AI_music', musicService.ok, musicService.ok ? `${musicService.url || '127.0.0.1:8793'} · Seed-VC 歌声转换` : (musicService.error || '本地桥接未启动')],
    ['三阶段工程', projects.ready, projects.ready ? (projects.busy ? '正在处理歌曲阶段' : '分离、推理、混音均可用') : '工程管线未就绪']
  ].map(([name,ok,detail])=>`<div class="pill"><div><strong>${escapeHtml(name)}</strong><div class="memory-meta">${escapeHtml(detail)}</div></div><span class="${ok?'ok':'bad'}">${ok?'就绪':'未就绪'}</span></div>`).join('');
  const referenceSelect = $('singingReferencePath');
  if(referenceSelect) {
    const previousReference = referenceSelect.value;
    referenceSelect.innerHTML = (data.singing_references || []).map(item=>`<option value="${escapeHtml(item.path)}">${escapeHtml(item.relative_path || item.name)}</option>`).join('');
    if((data.singing_references || []).some(item=>item.path===previousReference)) referenceSelect.value = previousReference;
    if(!referenceSelect.options.length) referenceSelect.innerHTML = '<option value="">没有找到唱歌素材</option>';
  }
}
async function uploadSingingSource() {
  const file = $('singingSourceFile').files[0];
  if(!file) return toast('先选择导唱或歌曲音频');
  const out = $('musicProjectState');
  out.textContent = '正在上传原曲到固定素材库...';
  const form = new FormData();
  form.append('file', file);
  try {
    const result = await api('/api/music/source', {method:'POST', body:form});
    $('singingSourcePath').value = result.path || '';
    await loadMusicSources(result.path || '');
    out.textContent = `原曲已进入 QQmusic-MP3 素材库：${result.name || file.name}`;
  } catch(error) {
    out.textContent = '上传失败：' + error.message;
  }
}
function deliverySourceTitle(path) {
"""
