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
  const requested = raw === 'memory' ? 'users' : raw;
  if(!requested) return;
  const button = Array.from(document.querySelectorAll('.tab')).find(item =>
    item.getAttribute('onclick') === `showTab(event,'${requested}')`
  );
  if(button) button.click();
}
setupAdvancedAccordions();
openRequestedTab();
loadStatus(); loadProfiles(); loadConfig(); loadProactive(); loadUserDirectory(false); loadModalityAccess();
// 记忆是用户卡片的核心数据，立即并行加载。
loadMemory().catch(error=>{ if($('userDirectoryStatus')) $('userDirectoryStatus').textContent='记忆读取失败：'+error.message; });
setInterval(()=>{
  if($('evaluation')?.classList.contains('active') && $('evaluationDashboardPanel')?.classList.contains('active')) {
    // 系统评测任务是异步旁路；看板定时刷新队列、趋势和明细，避免手动点刷新才能看到结果。
    loadEvaluation(false);
  }
}, 3000);
</script>
</body>
</html>"""
