from __future__ import annotations

PART = r"""        <div class="surface">
          <div class="section-head">
            <div><h2>统一用户中心</h2><p class="note">用户、群聊、记忆与互动设置集中管理。亚托莉始终置顶，余弦寄长风其次。</p></div>
            <div class="row"><button class="ghost" onclick="runUserDirectoryRefresh(this)">刷新用户目录</button></div>
          </div>
          <div class="user-directory-integrated-toolbar">
            <strong>会话状态</strong>
            <button onclick="runUserDirectoryRefresh(this)">刷新用户目录与群聊计划</button>
            <label class="switch-line"><input id="directoryProactiveEnabled" type="checkbox" onchange="saveDirectoryProactiveToggle()">主动消息</label>
            <span id="directoryProactiveState" class="note">正在读取主动消息状态...</span>
          </div>
          <div class="user-directory-integrated-toolbar modality-policy-toolbar">
            <strong>视觉 / 语音权限</strong>
            <label class="switch-line"><input id="directoryVisionAllowlistEnabled" type="checkbox" onchange="saveDirectoryModalityPolicy()">仅允许勾选用户使用视觉模型</label>
            <label class="switch-line"><input id="directoryVoiceAllowlistEnabled" type="checkbox" onchange="saveDirectoryModalityPolicy()">仅允许勾选用户使用语音模型</label>
            <span id="directoryModalityAccessState" class="note">正在读取权限策略...</span>
          </div>
          <div class="toolbar">
            <label>搜索昵称 / 备注 / QQ<input id="userDirectorySearch" placeholder="输入 QQ 号、昵称、备注或群名片" oninput="renderUserDirectory()"></label>
            <label>来源<select id="userDirectorySource" onchange="renderUserDirectory()"><option value="">全部来源</option><option value="memory">记忆</option><option value="insights">群聊总结</option><option value="modality">视觉/语音权限</option></select></label>
            <label>对象类型<select id="userDirectoryEntityType" onchange="renderUserDirectory()"><option value="all" selected>用户与群聊</option><option value="user">用户</option><option value="group">群聊</option></select></label>
          </div>
          <p class="note" id="userDirectoryStatus">正在读取统一用户目录...</p>
          <div class="scroll user-directory-scroll"><div id="userDirectoryRows" class="user-directory-list"><div class="empty">正在读取...</div></div></div>
        </div>
      </div>

      <div id="test" class="panel">
        <div class="stack">
          <div class="surface">
            <div class="section-head">
              <div><h2>开发者状态与访问入口</h2><p class="note">MaiBot 是独立服务（默认端口 8001），不会被 ATRI WebUI 代替；NapCat Token 仅在本机展示。</p></div>
              <div class="row"><button class="ghost" onclick="loadDeveloper(true)">刷新状态</button></div>
            </div>
            <div id="developerLinks" class="developer-links"><span class="note">正在读取本机服务信息...</span></div>
          </div>
          <div class="surface">
            <div class="section-head">
              <div><h2>实时项目日志</h2><p class="note" id="developerLogHint">每 3 秒读取一次；聊天正文和 Token 不写入统一日志。</p></div>
              <div class="row"><label>级别<select id="developerLogLevel" onchange="loadDeveloperLogs()"><option value="all">全部</option><option value="debug">Debug</option><option value="info">Info</option><option value="warning">Warn</option><option value="error">Error</option></select></label><button class="ghost" onclick="loadDeveloperLogs()">刷新日志</button></div>
            </div>
            <pre id="developerLog" class="log-view">日志加载中...</pre>
          </div>
          <div class="surface">
            <h2>聊天回复测试</h2>
            <textarea id="testText" placeholder="例如：我今天好累，亚托莉你会怎么回？"></textarea>
            <div class="row"><button onclick="testChat()">生成回复</button></div>
            <div class="out" id="testOut"></div>
          </div>
        </div>
      </div>

      <div id="evaluation" class="panel">
"""
