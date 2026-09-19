from __future__ import annotations

PART = r"""        <div class="stack">
          <div class="surface voice-module-hero music">
            <div class="section-head">
              <div>
                <h2>歌曲合成</h2>
                <p class="note">由 <span class="mono">AI_music</span> 负责导唱处理、Seed-VC 歌声转换、伴奏混音和任务管理；音源目录与素材库保持同步。</p>
              </div>
              <div class="row"><button class="ghost" onclick="loadVoice()">刷新歌曲状态</button></div>
            </div>
            <div id="musicServiceStatus" class="voice-status"></div>
          </div>
          <div class="surface">
            <ol class="stepper" aria-label="歌曲处理流程">
              <li class="active">人声分离</li>
              <li>模型推理</li>
              <li>混音与导出</li>
            </ol>
          </div>

          <div class="surface music-primary-action">
            <div class="section-head">
              <div>
                <h2>QQ 整首歌曲库</h2>
                <p class="note">QQ 点歌只从 <span class="mono">data/music/完整音乐</span> 按文件名匹配歌曲，并把对应完整文件作为 QQ 语音发送。放入或导入这里后即可点歌，不需要再切片。</p>
              </div>
              <div class="row"><button class="ghost" onclick="loadDeliverySongs()">刷新整首歌曲库</button></div>
            </div>
            <div class="music-delivery-grid">
              <div class="music-import-box">
                <strong>导入完整音乐</strong>
                <span class="param-help">支持 WAV、FLAC、MP3、OGG、M4A、AAC、OPUS；歌名默认使用文件名。</span>
                <input id="deliveryMusicFile" type="file" accept=".wav,.flac,.mp3,.ogg,.m4a,.aac,.opus,audio/*">
                <div class="row"><button class="ghost" onclick="uploadDeliveryMusic()">导入完整音乐</button></div>
                <p id="completeMusicState" class="note">目录中的完整歌曲会直接进入 QQ 点歌清单。</p>
              </div>
              <div>
                <strong>可点播的完整歌曲（QQ 实际发送来源）</strong>
                <div id="completeMusicList" class="music-delivery-list"><p class="note">正在读取完整歌曲…</p></div>
              </div>
            </div>
          </div>

          <div class="surface music-primary-action">
            <div class="section-head">
              <div><h2>新建歌曲工程</h2><p class="note">上传一次原曲，然后依次完成人声分离、模型推理和后期混音。每个阶段都会保存产物，可单独试听和重新运行。</p></div>
            </div>
            <div class="grid">
              <label>歌曲名称<input id="singingSongName" value="亚托莉歌曲工程" maxlength="120"></label>
              <label>亚托莉歌声参考<select id="singingReferencePath"></select></label>
              <label>素材库原曲<select id="musicSourceLibrary" onchange="selectMusicSource()"><option value="">从固定目录选择，或在下方上传</option></select><span class="param-help">固定目录：&lt;Music&gt;\QQmusic-MP3\</span></label>
              <label>原曲文件路径<input id="singingSourcePath" readonly placeholder="上传或从素材库选择后自动填写"></label>
              <label>乐理分段数量<select id="musicSectionCount"><option value="0">自动（推荐，3–5 段）</option><option value="3">固定 3 段</option><option value="4">固定 4 段</option><option value="5">固定 5 段</option></select><span class="param-help">推荐值：自动。依据节拍、小节、和声色度、音色与能量变化寻找边界，不按固定秒数切割。</span></label>
              <label>工程流程<select id="musicWorkflowMode"><option value="guided">分阶段确认（推荐）</option><option value="automatic">全自动三阶段</option></select><span class="param-help">全自动遇到质量检查失败仍会暂停，避免坏结果继续混音。</span></label>
            </div>
            <div class="row">
              <input id="singingSourceFile" type="file" accept=".wav,.flac,.mp3,.ogg,.m4a,.aac,audio/*">
              <button class="ghost" onclick="uploadSingingSource()">上传原曲</button>
              <button id="musicCreateProjectButton" onclick="createMusicProject()">创建三阶段工程</button>
            </div>
            <p class="note">上传只创建可恢复的歌曲工程，不覆盖成品。确认导出后才保存到 <span class="mono">&lt;Music&gt;\AI合成音乐</span>。</p>
          </div>

          <div class="surface">
            <div class="music-project-toolbar">
              <label>当前歌曲工程<select id="musicProjectSelect" onchange="selectMusicProject()"><option value="">尚未创建工程</option></select></label>
              <div class="row"><button id="musicRunPipeline" onclick="runMusicPipeline()">运行完整三阶段</button><button class="ghost" onclick="loadMusicProjects()">刷新工程</button></div>
            </div>
            <div class="natural-box" id="musicProjectState">创建工程后，可以逐阶段生成并试听。</div>
            <div id="musicSectionList" class="music-section-list"><div class="note">完成人声分离后显示自动乐理分段。</div></div>
            <div class="music-revision-bar"><label>历史版本<select id="musicRevisionSelect"><option value="">暂无版本</option></select></label><button class="ghost" onclick="rollbackMusicProject()">恢复选中版本</button><button class="ghost" onclick="recoverMusicProject()">恢复最近自动备份</button></div>
          </div>

          <div class="music-stage-grid">
            <div id="musicStageSeparation" class="music-stage-card">
              <div class="music-stage-head">
                <h3><span class="music-stage-index">1</span>人声分离</h3>
                <span id="musicSeparationState" class="music-stage-state">等待</span>
              </div>
              <div class="music-import-box">
                <strong>导入已分离音频（跳过模型分离）</strong>
                <span class="param-help">适合使用剪映等工具得到的干净分轨。主唱与伴奏必选，和声可选；导入后仍会自动生成乐理分段与乐句计划。</span>
                <div class="grid">
                  <label>主唱（必选）<input id="musicImportedVocalFile" type="file" accept=".wav,.flac,.mp3,.ogg,.m4a,.aac,audio/*"></label>
                  <label>伴奏（必选，保持立体声）<input id="musicImportedInstrumentalFile" type="file" accept=".wav,.flac,.mp3,.ogg,.m4a,.aac,audio/*"></label>
                  <label>和声（可选）<input id="musicImportedHarmonyFile" type="file" accept=".wav,.flac,.mp3,.ogg,.m4a,.aac,audio/*"></label>
                </div>
                <div class="music-stage-actions"><button id="musicImportStemsButton" class="ghost" onclick="importMusicStems()">导入已分离音频</button></div>
              </div>
              <div class="music-stage-layout">
                <div>
                  <p class="note">自动乐理分析后，将结构边界吸附到换气/低能量点，输出主唱、和声与立体声伴奏。</p>
                  <div class="grid">
                    <label>分离模型<select id="musicSeparationModel"><option value="mdx_net">MDX-Net（低资源）</option><option value="bs_roformer">BS-Roformer（高质量）</option><option value="demucs">Demucs v4（不同架构）</option></select><span class="param-help">本机推荐先用 MDX；BS-Roformer 权重已就绪；Demucs 首次选择会获取模型权重。</span></label>
                    <label>质量预设<select id="musicSeparationPreset"><option value="quick">快速</option><option value="standard" selected>标准（推荐）</option><option value="extreme">极致</option></select><span class="param-help">极致会提高重叠和 shifts，只建议最终成片使用。</span></label>
                    <label><span>和声处理</span><span class="row"><input id="musicSeparateHarmony" type="checkbox" checked> 分离主唱与和声</span><span class="param-help">快速模式会安全输出空和声轨，避免额外模型负载。</span></label>
                  </div>
                  <div class="music-stage-actions"><button id="musicRunSeparation" onclick="runMusicStage('separation')">开始人声分离</button><button class="ghost" onclick="confirmMusicStage('separation')">确认并继续</button><button class="ghost" onclick="resetMusicStage('separation')">重置本阶段</button></div>
                </div>
                <div>
                  <div class="music-track"><strong>主唱（单声道）</strong><canvas id="musicVocalWave" width="900" height="96" class="music-waveform"></canvas><button class="ghost music-preview-button" onclick="loadMusicPreview('musicVocalAudio','musicVocalWave','vocal')">按需加载试听与波形</button><audio id="musicVocalAudio" controls preload="none"></audio></div>
                  <div class="music-track"><strong>和声</strong><canvas id="musicHarmonyWave" width="900" height="96" class="music-waveform"></canvas><button class="ghost music-preview-button" onclick="loadMusicPreview('musicHarmonyAudio','musicHarmonyWave','harmony')">按需加载试听与波形</button><audio id="musicHarmonyAudio" controls preload="none"></audio></div>
                  <div class="music-track"><strong>伴奏（保持立体声）</strong><canvas id="musicInstrumentalWave" width="900" height="96" class="music-waveform"></canvas><button class="ghost music-preview-button" onclick="loadMusicPreview('musicInstrumentalAudio','musicInstrumentalWave','instrumental')">按需加载试听与波形</button><audio id="musicInstrumentalAudio" controls preload="none"></audio></div>
                </div>
              </div>
            </div>

            <div id="musicStageInference" class="music-stage-card">
              <div class="music-stage-head">
                <h3><span class="music-stage-index">2</span>模型推理</h3>
                <span id="musicInferenceState" class="music-stage-state">等待</span>
              </div>
              <p class="note">Seed-VC 44.1k 歌声模型使用 RMVPE 提取 F0；每组保留 2–4 秒上下文并以 equal-power overlap-add 拼接。</p>
              <div class="music-control-grid">
                <label>移调（半音）<input id="musicPitchShift" type="number" min="-12" max="12" step="0.5" value="0"><span class="param-help">推荐值：0。每 ±12 为一个八度；音区不合适时先在 -2～+2 内试听。</span></label>
                <label>推理步数<input id="musicDiffusionSteps" type="number" min="10" max="50" value="35"><span class="param-help">参考：20 快速预览，35 推荐，45–50 更细致但更慢、占用更高。</span></label>
                <label>演唱质感<select id="musicStyle"><option value="natural">自然（推荐）</option><option value="gentle">温柔</option><option value="bright">明亮</option><option value="soft">轻柔</option></select><span class="param-help">调整 Seed-VC 引导强度：自然 0.70、温柔 0.62、明亮 0.82、轻柔 0.56。</span></label>
                <label>上下文（秒）<input id="musicContextSeconds" type="number" min="2" max="4" step="0.5" value="3"><span class="param-help">推荐 3 秒；越长越平滑但推理更慢。</span></label>
              </div>
              <div class="music-stage-actions"><button id="musicRunInference" onclick="runMusicStage('inference')">开始整曲模型推理</button><button class="ghost" onclick="confirmMusicStage('inference')">确认并继续</button><button class="ghost" onclick="resetMusicStage('inference')">重置本阶段</button></div>
              <div class="music-track"><strong>亚托莉转换人声（拖动波形选择局部）</strong><canvas id="musicConvertedWave" width="1200" height="110" class="music-waveform selectable"></canvas><button class="ghost music-preview-button" onclick="loadMusicPreview('musicConvertedAudio','musicConvertedWave','converted',true)">按需加载可编辑波形与试听</button><audio id="musicConvertedAudio" controls preload="none"></audio></div>
              <div id="musicInferenceQuality" class="music-quality"><div class="note">整曲推理后显示接缝、静音、音高与削波检查。</div></div>
              <div class="music-editor">
                <strong>局部片段精修</strong><span class="param-help">可拖动上方波形，或直接输入时间；只替换选区，选区外采样保持不变。</span>
                <div class="music-range-grid">
                  <label>开始（秒或 MM:SS）<input id="musicSelectionStart" type="text" inputmode="decimal" placeholder="0 或 2:58" value="0"></label>
                  <label>结束（秒或 MM:SS）<input id="musicSelectionEnd" type="text" inputmode="decimal" placeholder="15 或 3:01" value="15"></label>
                  <label>情感强度<input id="musicEmotionStrength" type="range" min="0" max="1" step="0.01" value="0.55"><span class="param-help">参考 0.45–0.65。</span></label>
                  <label>气声比例<input id="musicBreathiness" type="range" min="0" max="1" step="0.01" value="0.02"><span class="param-help">整曲推荐 0–0.03；只在检测到的人声区域生效，不再放大纯音乐残留。</span></label>
                  <label>颤音<input id="musicVibrato" type="range" min="0" max="1" step="0.01" value="0"><span class="param-help">整曲推荐 0。模型已保留原唱颤音；选中长音后可局部使用 0.03–0.10。</span></label>
                  <label>咬字<input id="musicArticulation" type="range" min="0" max="1" step="0.01" value="0.60"><span class="param-help">日语 0.55–0.65；英语 0.60–0.72。</span></label>
                  <label>共振峰<input id="musicFormantShift" type="number" min="-4" max="4" step="0.1" value="0.6"><span class="param-help">ATRI 参考 +0.3～+1.2，轻微年轻化。</span></label>
                  <label>片段音调（半音）<input id="musicSegmentPitch" type="number" min="-12" max="12" step="0.5" value="0"></label>
                </div>
                <div class="music-stage-actions"><button id="musicRerunSegment" onclick="rerunMusicSegment()">只重生成选中片段</button><button class="ghost" onclick="previewSelectedMusicRange()">试听选区</button></div>
              </div>
            </div>

            <div id="musicStageMix" class="music-stage-card">
              <div class="music-stage-head">
                <h3><span class="music-stage-index">3</span>后期混音</h3>
                <span id="musicMixState" class="music-stage-state">等待</span>
              </div>
              <p class="note">Pedalboard：高通 → 动态 EQ → 齿音抑制 → 压缩 → 饱和 → Reverb/Delay → Limiter；人声单声道，伴奏保持立体声。</p>
              <div class="music-control-grid">
                <label>人声增益 dB<input id="musicVocalGain" type="number" min="-12" max="12" step="0.5" value="0"><span class="param-help">推荐值：0 dB。人声靠后可加 +1～+3 dB，刺耳或过近可减 -1～-3 dB。</span></label>
                <label>伴奏增益 dB<input id="musicInstrumentalGain" type="number" min="-18" max="6" step="0.5" value="-1.7"><span class="param-help">推荐 -1.7 dB；人声被盖住可降至 -3～-5 dB。</span></label>
                <label>Ducking dB<input id="musicDucking" type="number" min="0" max="12" step="0.5" value="3"><span class="param-help">推荐 2–4 dB，按人声包络自动闪避伴奏。</span></label>
                <label>原和声增益 dB<input id="musicHarmonyGain" type="number" min="-120" max="0" step="1" value="-120"><span class="param-help">默认 -120（静音），避免原唱经和声轨泄漏；确认和声纯净后可尝试 -18～-10 dB。</span></label>
                <label>存在感 EQ dB<input id="musicEqPresence" type="number" min="-9" max="6" step="0.5" value="-1"><span class="param-help">2.5–5.2 kHz 动态处理；机械刺耳时降低。</span></label>
                <label>空气感 EQ dB<input id="musicEqAir" type="number" min="-6" max="9" step="0.5" value="1.2"><span class="param-help">9 kHz 高架；ATRI 建议 +0.5～+2 dB。</span></label>
                <label>压缩阈值 dB<input id="musicCompressorThreshold" type="number" min="-40" max="-6" step="1" value="-18"></label>
                <label>压缩比<input id="musicCompressorRatio" type="number" min="1" max="10" step="0.1" value="2.5"><span class="param-help">推荐 2:1～3:1。</span></label>
                <label>齿音抑制<input id="musicDeesser" type="range" min="0" max="1" step="0.01" value="0.35"><span class="param-help">推荐 0.25–0.45。</span></label>
                <label>饱和 drive dB<input id="musicSaturation" type="number" min="0" max="9" step="0.5" value="1"><span class="param-help">推荐 0.5–1.5 dB。</span></label>
                <label>混响<input id="musicReverb" type="range" min="0" max="0.45" step="0.01" value="0.08"><span class="param-help">推荐值：0.08。自然空间感通常 0.05–0.12；0 为完全干声。</span></label>
                <label>延迟 ms<input id="musicDelayMs" type="number" min="0" max="500" step="5" value="90"></label>
                <label>延迟比例<input id="musicDelayMix" type="range" min="0" max="0.35" step="0.01" value="0.04"><span class="param-help">推荐 0.02–0.08。</span></label>
              </div>
              <div class="music-stage-actions"><button id="musicRunMix" onclick="runMusicStage('mix')">生成工程内混音</button><button class="ghost" onclick="confirmMusicStage('mix')">确认混音</button><button class="ghost" onclick="resetMusicStage('mix')">重置本阶段</button></div>
              <div class="music-track"><strong>最终混音</strong><canvas id="musicMixWave" width="1200" height="96" class="music-waveform"></canvas><button class="ghost music-preview-button" onclick="loadMusicPreview('musicMixAudio','musicMixWave','mix')">按需加载试听与波形</button><audio id="musicMixAudio" controls preload="none"></audio></div>
              <div class="music-editor">
                <strong>导出成品</strong>
                <div class="music-control-grid">
                  <label>导出预设<select id="musicExportPreset"><option value="preview">试听</option><option value="live" selected>直播</option><option value="conservative_master">保守母带</option><option value="custom">自定义</option></select></label>
                  <label>格式<select id="musicExportFormat"><option value="wav">WAV 24-bit</option><option value="flac">FLAC</option><option value="mp3">MP3</option></select></label>
                  <label>目标响度 LUFS<input id="musicExportLufs" type="number" min="-24" max="-8" step="0.5" value="-14"><span class="param-help">直播 -14；试听 -16；保守母带 -13。</span></label>
                </div>
                <div class="music-stage-actions"><button id="musicExportButton" onclick="exportMusicProject()">导出到 AI合成音乐</button></div>
              </div>
            </div>
          </div>
          <div class="surface">
            <div class="section-head"><div><h2>合成终端 · 简洁进度</h2><p class="note">这里只显示阶段、百分比与可读字幕；模型原始调试日志写入歌曲工程 logs/*.debug.log，不刷屏、不影响桌面。</p></div></div>
            <pre id="musicTerminal" class="music-terminal">选择歌曲工程后显示阶段字幕。</pre>
          </div>

          <div class="surface">
            <div class="section-head">
              <div>
                <h2>QQ 唱歌片段库（独立管理区）</h2>
                <p class="note">此区域仅用于旧版分段文件的生成、管理和试听，位于歌曲合成面板最底部以便区分。QQ 点歌不会读取 <span class="mono">data/music/完整音乐片段</span>，也不会发送这里的片段。</p>
              </div>
              <div class="row"><button class="ghost" onclick="loadDeliverySongs()">刷新片段库</button></div>
            </div>
            <div class="music-delivery-grid">
              <div class="music-import-box">
                <strong>从完整音乐生成试听片段</strong>
                <label>选择完整歌曲<select id="deliveryMusicSource"><option value="">先导入或刷新列表</option></select></label>
                <label>片段库显示歌名<input id="deliveryMusicTitle" maxlength="80" placeholder="默认使用文件名"></label>
                <label>目标片段时长<select id="deliveryMusicTarget"><option value="30" selected>约 30 秒（推荐）</option><option value="25">约 25 秒</option><option value="35">约 35 秒</option></select></label>
                <div class="row"><button id="deliveryMusicSegmentButton" class="ghost" onclick="segmentDeliveryMusic()">分段并保存到独立片段库</button></div>
                <p class="param-help">同名歌曲会安全覆盖旧片段；保存位置：<span id="deliveryMusicOutputPath" class="mono">data/music/完整音乐片段</span></p>
                <p id="deliveryMusicState" class="note">这些片段只用于 WebUI 管理和试听。</p>
              </div>
              <div>
                <strong>已有片段（不会由 QQ 发送）</strong>
                <div id="deliveryMusicList" class="music-delivery-list"><p class="note">正在读取片段库…</p></div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div id="users" class="panel">
"""
