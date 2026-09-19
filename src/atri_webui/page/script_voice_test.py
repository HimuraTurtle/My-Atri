from __future__ import annotations

PART = r"""  $('testOut').textContent='亚托莉生成中...';
  const r = await api('/api/test-chat', {method:'POST', body:JSON.stringify({text:$('testText').value})});
  $('testOut').textContent = r.used_ai
    ? r.reply
    : `当前聊天模型没有连上，正在走兜底。
错误：${r.error || '未知'}

兜底回复：${r.reply}`;
}
async function restartServices() {
  const r = await api('/api/restart', {method:'POST', body:'{}'});
  toast(r.message || r.error || '已执行');
}
function setupAdvancedAccordions() {
  document.querySelectorAll('#advanced .accordion-section').forEach(section=>{
    const heading = section.querySelector(':scope > .section-head') || section.querySelector(':scope > h3');
    if(!heading) return;
    section.classList.add('collapsed');
    heading.setAttribute('role','button');
    heading.setAttribute('tabindex','0');
    const toggle = ()=>section.classList.toggle('collapsed');
    heading.addEventListener('click', event=>{
      if(event.target.closest('button')) return;
      toggle();
    });
    heading.addEventListener('keydown', event=>{
      if(event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
    });
  });
}
function openRequestedTab() {
  const raw = new URLSearchParams(window.location.search).get('tab');
  const requested = ['memory','qzone'].includes(raw) ? 'users' : raw;
  if(!requested) return;
  const button = Array.from(document.querySelectorAll('.tab')).find(item =>
    item.getAttribute('onclick') === `showTab(event,'${requested}')`
  );
  if(button) button.click();
}
setupAdvancedAccordions();
openRequestedTab();
loadStatus(); loadProfiles(); loadConfig(); loadProactive(); loadUserDirectory(false); loadModalityAccess();
// 目录核心数据先显示；本地自动发布状态与 QQ 动态优先读取。完整记忆摘要
// 的构建较重，等 QQ 动态完成或最多等待 25 秒后再后台加载，避免争抢首屏。
loadDirectoryAutomationState();
// 记忆是用户卡片的核心数据，立即并行加载；QQ 空间慢请求不再阻塞它。
loadMemory().catch(error=>{ if($('userDirectoryStatus')) $('userDirectoryStatus').textContent='记忆读取失败：'+error.message; });
refreshDirectoryQZoneData();
setInterval(()=>{
  if($('evaluation')?.classList.contains('active') && $('evaluationDashboardPanel')?.classList.contains('active')) {
    // 系统评测任务是异步旁路；看板定时刷新队列、趋势和明细，避免手动点刷新才能看到结果。
    loadEvaluation(false);
  }
}, 3000);
// Do not refresh the QQ 动态 page on a fixed hourly cadence. We only check a
// lightweight publication timestamp and rebuild the page after a real post;
// normal viewing/refreshing therefore leaves the persisted schedule untouched.
</script>
</body>
</html>"""
