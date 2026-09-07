/**
 * 日期选择器增强（air-datepicker）
 * ==========================================
 * 将页面所有 <input type="date"> 替换为 air-datepicker 日期选择器：
 * - 单日历模式：点击输入框弹出日历选择日期，选中后自动关闭
 * - 中文界面（内置 zh 语言包）、深色主题（css/lib/air-datepicker-dark.css）
 * - 保留原生 min/max 限制（过去/超出范围日期不可选）
 * - 默认值 = input 现有 value（不改默认选择）
 * - 选中后同步 input.value 并触发 change（现有 JS 无感）
 */
(function () {
  'use strict';

  // 中文语言包（对应 air-datepicker locale/zh.js）
  var zhLocale = {
    days: ['周日', '周一', '周二', '周三', '周四', '周五', '周六'],
    daysShort: ['日', '一', '二', '三', '四', '五', '六'],
    daysMin: ['日', '一', '二', '三', '四', '五', '六'],
    months: ['一月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '十一月', '十二月'],
    monthsShort: ['一月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '十一月', '十二月'],
    today: '今天',
    clear: '清除',
    dateFormat: 'yyyy-MM-dd',
    timeFormat: 'HH:mm',
    firstDay: 1
  };

  function init() {
    if (typeof AirDatepicker === 'undefined') {
      console.warn('[date-picker] air-datepicker 未加载，跳过日期增强');
      return;
    }
    document.querySelectorAll('input[type="date"]').forEach(function (input) {
      if (input.dataset.adpInit) return;
      input.dataset.adpInit = '1';
      try {
        // 改为文本输入框，避免浏览器原生日期控件与日历弹层冲突
        input.type = 'text';

        var dp = new AirDatepicker(input, {
          dateFormat: 'yyyy-MM-dd',
          locale: zhLocale,
          autoClose: true,          // 选中后自动关闭
          minDate: input.min || undefined,
          maxDate: input.max || undefined,
          onSelect: function (params) {
            // params.formattedDate 为格式化后的日期字符串
            input.value = params.formattedDate || '';
            input.dispatchEvent(new Event('change', { bubbles: true }));
          }
        });
        input._airdatepicker = dp;
      } catch (e) {
        console.warn('[date-picker] 初始化失败:', input.id, e);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
