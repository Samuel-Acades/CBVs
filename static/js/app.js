function copyNames() {
  var box = document.getElementById('nameList');
  if (!box) return;
  var text = box.textContent.trim();
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(flash);
  } else {
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    flash();
  }
  function flash() {
    var btn = event && event.target;
    if (btn) {
      var old = btn.textContent;
      btn.textContent = 'Copied ✓';
      setTimeout(function () { btn.textContent = old; }, 1400);
    }
  }
}

(function initSidebar() {
    var sidebar = document.getElementById('sidebar');
    var toggle = document.getElementById('sidebarToggle');
    if (!sidebar || !toggle) return;

    var storageKey = 'cbv-sidebar-collapsed';
    try {
      document.body.classList.toggle('sidebar-collapsed', localStorage.getItem(storageKey) === 'true');
    } catch (error) {
      // Keep the sidebar usable when browser storage is disabled.
    }

    function updateButton() {
      var collapsed = document.body.classList.contains('sidebar-collapsed');
      toggle.textContent = collapsed ? '›' : '‹';
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
      toggle.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    }

    toggle.addEventListener('click', function () {
      var collapsed = document.body.classList.toggle('sidebar-collapsed');
      try {
        localStorage.setItem(storageKey, String(collapsed));
      } catch (error) {
        // Toggling remains functional for this page without persistent storage.
      }
      updateButton();
    });
    updateButton();
})();

var chartSvg = 'http://www.w3.org/2000/svg';
var chartColors = ['#018d43', '#99cc33', '#9ca3af', '#6b7280'];

function chartElement(name, attributes) {
    var node = document.createElementNS(chartSvg, name);
    Object.keys(attributes || {}).forEach(function (key) {
      node.setAttribute(key, attributes[key]);
    });
    return node;
}

function chartText(parent, x, y, value, attributes) {
    var node = chartElement('text', Object.assign({ x: x, y: y }, attributes || {}));
    node.textContent = value == null ? '' : String(value);
    parent.appendChild(node);
    return node;
}

function chartTitle(parent, value) {
    var title = chartElement('title');
    title.textContent = value;
    parent.appendChild(title);
}

function chartCanvas(id, width, height, label, role) {
    var host = document.getElementById(id);
    if (!host) return null;
    host.replaceChildren();
    var svg = chartElement('svg', {
      viewBox: '0 0 ' + width + ' ' + height,
      role: role || 'img',
      'aria-label': label,
      preserveAspectRatio: 'xMidYMid meet'
    });
    host.appendChild(svg);
    return svg;
}

function watchChartResize(id, redraw) {
    var host = document.getElementById(id);
    if (!host) return;
    host._redrawChart = redraw;
    if (typeof ResizeObserver === 'undefined' || host._chartResizeObserver) return;
    host._chartResizeObserver = new ResizeObserver(function () {
      if (host.clientWidth && host._redrawChart) host._redrawChart();
    });
    host._chartResizeObserver.observe(host);
}

function renderTrendChart(id, data) {
    var labels = data.labels || [];
    if (!labels.length) return;
    var host = document.getElementById(id);
    watchChartResize(id, function () { renderTrendChart(id, data); });
    var width = Math.max(240, host ? host.clientWidth : 760), height = 300;
    var left = 42, right = 12, top = 18, bottom = 70;
    var plotWidth = width - left - right, plotHeight = height - top - bottom;
    var encounters = data.encounters || [];
    var submissions = data.submissions || [];
    var line = data.line || [];
    var values = encounters.concat(submissions, line).filter(function (v) { return Number.isFinite(v); });
    var max = Math.max(1, ...values);
    var step = plotWidth / labels.length;
    var svg = chartCanvas(id, width, height, 'Monthly encounters, reports, and projection', data.drilldown ? 'group' : 'img');
    if (!svg) return;

    for (var tick = 0; tick <= 4; tick++) {
      var y = top + plotHeight - (tick / 4) * plotHeight;
      svg.appendChild(chartElement('line', { x1: left, y1: y, x2: width - right, y2: y, stroke: '#e5e7eb', 'stroke-width': 1 }));
      chartText(svg, left - 8, y + 4, Math.round(max * tick / 4), { 'text-anchor': 'end', fill: '#6b7280', 'font-size': 11 });
    }

    labels.forEach(function (label, index) {
      var center = left + step * (index + .5);
      var barWidth = Math.min(18, step * .24);
      [
        [encounters[index], chartColors[0], -barWidth / 2, 'Encounters'],
        [submissions[index], chartColors[1], barWidth / 2, 'Reports submitted']
      ].forEach(function (series) {
        if (!Number.isFinite(series[0])) return;
        var barHeight = (series[0] / max) * plotHeight;
        var bar = chartElement('rect', {
          x: center + series[2], y: top + plotHeight - barHeight,
          width: barWidth - 2, height: Math.max(0, barHeight), rx: 3, fill: series[1]
        });
        chartTitle(bar, String(label) + ' — ' + series[3] + ': ' + series[0]);
        if (data.drilldown && data.months && data.months[index] && series[0] > 0) {
          var target = '/month-details?month=' + encodeURIComponent(data.months[index]) +
            '&kind=' + (series[3] === 'Encounters' ? 'encounters' : 'submissions');
          bar.setAttribute('role', 'link');
          bar.setAttribute('tabindex', '0');
          bar.setAttribute('aria-label', 'View ' + series[0] + ' ' + series[3].toLowerCase() + ' in ' + label);
          bar.style.cursor = 'pointer';
          bar.addEventListener('click', function () { window.location.href = target; });
          bar.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              window.location.href = target;
            }
          });
        }
        svg.appendChild(bar);
      });
      var shortLabel = String(label).replace(/\s+\(.*\)$/, '');
      if (shortLabel.length > 15) shortLabel = shortLabel.slice(0, 13) + '…';
      if (width >= 390 || index % Math.ceil(labels.length / Math.max(1, Math.floor(plotWidth / 42))) === 0) {
        chartText(svg, center, height - 48, shortLabel, { 'text-anchor': 'middle', fill: '#6b7280', 'font-size': width < 390 ? 9 : 10 });
      }
    });

    var lastActual = -1;
    encounters.forEach(function (value, index) { if (Number.isFinite(value)) lastActual = index; });
    var targetIndex = line.length - 1;
    if (lastActual >= 0 && targetIndex > lastActual && Number.isFinite(line[lastActual]) && Number.isFinite(line[targetIndex])) {
      var startX = left + step * (lastActual + .5);
      var endX = left + step * (targetIndex + .5);
      var startY = top + plotHeight - (line[lastActual] / max) * plotHeight;
      var endY = top + plotHeight - (line[targetIndex] / max) * plotHeight;
      svg.appendChild(chartElement('line', {
        x1: startX, y1: startY, x2: endX, y2: endY,
        stroke: chartColors[3], 'stroke-width': 2, 'stroke-dasharray': '6 5'
      }));
      svg.appendChild(chartElement('circle', { cx: endX, cy: endY, r: 4, fill: chartColors[3] }));
    }

    var legend = [
      ['Encounters', chartColors[0], 'rect'],
      ['Reports submitted', chartColors[1], 'rect'],
      ['Projection', chartColors[3], 'line']
    ];
    var compact = width < 390;
    var legendPositions = compact
      ? [6, width / 2 + 2, width / 2 - 44]
      : [Math.max(12, (width - 285) / 2), Math.max(12, (width - 285) / 2) + 95, Math.max(12, (width - 285) / 2) + 190];
    legend.forEach(function (item, index) {
      var x = legendPositions[index];
      var legendY = compact && index === 2 ? height - 9 : height - 25;
      if (item[2] === 'line') {
        svg.appendChild(chartElement('line', { x1: x, y1: legendY - 4, x2: x + 15, y2: legendY - 4, stroke: item[1], 'stroke-width': 2, 'stroke-dasharray': '4 3' }));
      } else {
        svg.appendChild(chartElement('rect', { x: x, y: legendY - 8, width: 10, height: 8, rx: 2, fill: item[1] }));
      }
      chartText(svg, x + 19, legendY, item[0], { fill: '#6b7280', 'font-size': compact ? 9 : 10 });
    });
}

function renderStatusChart(id, status) {
    var entries = [
      ['Recommended', Number(status.recommended || 0)],
      ['Received already', Number(status.received || 0)],
      ['No encounters', Number(status.no_report || 0)],
      ['Excluded', Number(status.excluded || 0)]
    ];
    var total = entries.reduce(function (sum, item) { return sum + item[1]; }, 0);
    var host = document.getElementById(id);
    watchChartResize(id, function () { renderStatusChart(id, status); });
    var compact = Boolean(host && host.clientWidth < 390);
    var width = compact ? Math.max(240, host.clientWidth) : 440, height = compact ? 300 : 280;
    var svg = chartCanvas(id, width, height, 'Bundle recommendation status');
    if (!svg) return;

    if (!total) {
      chartText(svg, width / 2, height / 2, 'No status data', { 'text-anchor': 'middle', fill: '#6b7280', 'font-size': 14 });
      return;
    }
    var cx = compact ? width / 2 : 126, cy = compact ? 92 : 132, radius = compact ? 68 : 84, inner = compact ? 42 : 52;
    var angle = -Math.PI / 2;
    entries.forEach(function (entry, index) {
      if (!entry[1]) return;
      var next = angle + entry[1] / total * Math.PI * 2;
      if (entry[1] === total) {
        svg.appendChild(chartElement('circle', { cx: cx, cy: cy, r: (radius + inner) / 2, fill: 'none', stroke: chartColors[index], 'stroke-width': radius - inner }));
      } else {
        var x1 = cx + radius * Math.cos(angle), y1 = cy + radius * Math.sin(angle);
        var x2 = cx + radius * Math.cos(next), y2 = cy + radius * Math.sin(next);
        var ix2 = cx + inner * Math.cos(next), iy2 = cy + inner * Math.sin(next);
        var ix1 = cx + inner * Math.cos(angle), iy1 = cy + inner * Math.sin(angle);
        var largeArc = next - angle > Math.PI ? 1 : 0;
        var path = 'M ' + x1 + ' ' + y1 + ' A ' + radius + ' ' + radius + ' 0 ' + largeArc + ' 1 ' + x2 + ' ' + y2 +
          ' L ' + ix2 + ' ' + iy2 + ' A ' + inner + ' ' + inner + ' 0 ' + largeArc + ' 0 ' + ix1 + ' ' + iy1 + ' Z';
        svg.appendChild(chartElement('path', { d: path, fill: chartColors[index], stroke: '#fff', 'stroke-width': 2 }));
      }
      angle = next;
    });
    chartText(svg, cx, cy + 5, total, { 'text-anchor': 'middle', fill: '#1f2933', 'font-size': 22, 'font-weight': 700 });
    chartText(svg, cx, cy + 22, 'CBVs', { 'text-anchor': 'middle', fill: '#6b7280', 'font-size': 11 });
    entries.forEach(function (entry, index) {
      var x = compact ? (index % 2 === 0 ? 2 : Math.round(width / 2) + 5) : 244;
      var y = compact ? 215 + Math.floor(index / 2) * 42 : 76 + index * 38;
      var labelX = x + 13;
      var countX = compact ? x + 108 : 416;
      svg.appendChild(chartElement('circle', { cx: x + 5, cy: y - 4, r: 5, fill: chartColors[index] }));
      chartText(svg, labelX, y, entry[0], { fill: '#4b5563', 'font-size': compact ? 10 : 12 });
      chartText(svg, countX, y, entry[1], { 'text-anchor': 'end', fill: '#1f2933', 'font-size': 12, 'font-weight': 650 });
    });
}

function renderTopChart(id, rows) {
    rows = rows || [];
    var host = document.getElementById(id);
    watchChartResize(id, function () { renderTopChart(id, rows); });
    var width = Math.max(240, host ? host.clientWidth : 760), rowHeight = 25, height = Math.max(140, rows.length * rowHeight + 28);
    var svg = chartCanvas(id, width, height, 'CBVs ranked by encounter count');
    if (!svg || !rows.length) return;
    var max = Math.max(1, ...rows.map(function (row) { return Number(row.encounters || 0); }));
    var labelX = 8, barX = Math.min(220, Math.round(width * .42)), barWidth = width - barX - 42;
    var maxLabelLength = Math.max(10, Math.floor((barX - labelX - 12) / 6));
    rows.forEach(function (row, index) {
      var y = 20 + index * rowHeight;
      var label = String(row.cbv || '');
      if (label.length > maxLabelLength) label = label.slice(0, maxLabelLength - 1) + '…';
      var labelNode = chartText(svg, labelX, y + 4, label, { fill: '#4b5563', 'font-size': 11 });
      var title = chartElement('title');
      title.textContent = row.cbv || '';
      labelNode.appendChild(title);
      var value = Number(row.encounters || 0);
      var length = value / max * barWidth;
      svg.appendChild(chartElement('rect', { x: barX, y: y - 7, width: barWidth, height: 13, rx: 4, fill: '#f0f2f0' }));
      if (length) {
        var bar = chartElement('rect', { x: barX, y: y - 7, width: length, height: 13, rx: 4, fill: index < 3 ? chartColors[0] : '#59ad78' });
        chartTitle(bar, row.cbv + ' — Encounters: ' + value);
        svg.appendChild(bar);
      }
      chartText(svg, barX + barWidth + 12, y + 4, value, { fill: '#1f2933', 'font-size': 11, 'font-weight': 650 });
    });
}

function renderCbvChart(id, data) {
    var labels = data.labels || [], values = data.values || [];
    if (!labels.length) return;
    renderTrendChart(id, {
      labels: labels, encounters: values, submissions: values.map(function () { return null; }), line: []
    });
}

function renderOverviewCharts(data) {
    renderTrendChart('trendChart', data.chart);
    renderStatusChart('statusChart', data.status);
    renderTopChart('topChart', data.top);
}
