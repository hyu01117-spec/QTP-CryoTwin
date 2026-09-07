/**
 * 流域/模型选择增强（Tom Select 单选下拉）
 * ==========================================
 * 对带 data-tomselect 标记的 <select> 初始化为 Tom Select 单选下拉组件：
 * - 单选、保持选项顺序、允许空值占位项（-- 请选择 --）
 * - 深色主题见 css/lib/tom-select-dark.css
 * - 兼容原生 select：选择后原生 select 的 value/selectedOptions 自动同步，
 *   并补触发原生 change 事件（现有 JS 无感）
 * - 动态选项：模拟页/风险评估页的流域、模型选项由 JS 从 API 异步填充，
 *   本脚本轮询原生 select 的选项，待选项稳定后初始化一次（此时 Tom Select
 *   会解析原生真实 <option> 元素），之后不再反复同步，避免选项重复累积。
 *   签名采用"顺序无关 + 忽略空值占位项"，因此 Tom Select 内部镜像对原生
 *   select 的调整（移动选中项/补建空 option）不会触发重建循环。
 */
(function () {
  'use strict';

  // 顺序无关、忽略空值占位项的签名
  function computeSig(select) {
    var parts = [];
    Array.prototype.forEach.call(select.options, function (o) {
      if (o.value === '') return;   // 忽略空值占位项（Tom Select 镜像可能补建）
      parts.push(o.value + '|' + o.text);
    });
    return parts.sort().join(';');
  }

  function createTomSelect(select) {
    var ts = new TomSelect(select, {
      create: false,
      allowEmptyOption: true,   // 保留 "-- 请选择 --" 空值占位项
      maxOptions: 1000,
      onChange: function () {
        // 原生 select 由 Tom Select 内部同步；补触发 change 供现有逻辑使用
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
    select.tomselect = ts;
    return ts;
  }

  function init() {
    if (typeof TomSelect === 'undefined') {
      console.warn('[tomselect-init] Tom Select 未加载，跳过');
      return;
    }
    document.querySelectorAll('select[data-tomselect]').forEach(function (select) {
      if (select.dataset.tsInit || select.dataset.tsPending) return;
      select.dataset.tsPending = '1';

      var attempts = 0;
      var stableTicks = 0;
      var lastSig = null;
      var timer = setInterval(function () {
        attempts++;
        var sig = computeSig(select);
        if (sig === lastSig) {
          stableTicks++;
        } else {
          lastSig = sig;
          stableTicks = 0;
        }
        // 选项稳定（连续 4 次不变）或超时后初始化一次
        if (stableTicks >= 4 || attempts >= 30) {
          clearInterval(timer);
          select.dataset.tsPending = '';
          try {
            createTomSelect(select);
            select.dataset.tsInit = '1';
          } catch (e) {
            console.warn('[tomselect-init] 初始化失败:', select.id, e);
            delete select.dataset.tsInit;
            return;
          }
          // 轻量监听：若原生选项在初始化后再次变化（极少见），销毁重建
          var lastAfterInit = computeSig(select);
          var watchAttempts = 0;
          var watch = setInterval(function () {
            watchAttempts++;
            var s = computeSig(select);
            if (s !== lastAfterInit) {
              lastAfterInit = s;
              try {
                if (select.tomselect) select.tomselect.destroy();
                createTomSelect(select);
              } catch (e) {
                console.warn('[tomselect-init]', select.id, '重建失败:', e);
              }
            }
            if (watchAttempts >= 25) { // 约 7.5 秒后停止监听
              clearInterval(watch);
            }
          }, 300);
        }
      }, 300);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  // 兜底：window load 后再初始化一次（处理 DOMContentLoaded 时脚本/样式未就绪的情况）
  window.addEventListener('load', function () {
    setTimeout(init, 100);
  });
})();
