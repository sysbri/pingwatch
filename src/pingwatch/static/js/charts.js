/* Chart.js wrappers for PingWatch.
 *
 * Each function returns a Chart instance so the caller may keep it alive and
 * call `update('none')` for tear-free live updates.
 */

(function (global) {
  function _maybeChart() {
    return typeof global.Chart !== 'undefined' ? global.Chart : null;
  }

  function latencyChart(canvas, dataPoints, outageMarkers, spikeMarkers) {
    const C = _maybeChart();
    if (!C || !canvas) return null;
    const fmtTime = (ts) => {
      if (!ts) return '';
      const d = new Date(ts);
      const pad = (n) => String(n).padStart(2, '0');
      return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    };
    return new C(canvas, {
      type: 'line',
      data: {
        labels: dataPoints.map((p) => p.ts_ms || p.ts || p.x || ''),
        datasets: [
          {
            label: 'Latenz',
            data: dataPoints.map((p) => (p.latency_us ? p.latency_us / 1000 : null)),
            borderColor: '#60a5fa',
            backgroundColor: 'rgba(96,165,250,0.10)',
            borderWidth: 1.5,
            tension: 0.2,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: '#60a5fa',
            pointHoverBorderColor: '#ffffff',
            pointHoverBorderWidth: 2,
            fill: true,
            spanGaps: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false, axis: 'x' },
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: true,
            mode: 'index',
            intersect: false,
            backgroundColor: 'rgba(15,22,32,0.95)',
            titleColor: '#ffffff',
            bodyColor: '#e5e7eb',
            borderColor: '#374151',
            borderWidth: 1,
            padding: 10,
            titleFont: { size: 13, weight: '600' },
            bodyFont: { size: 13 },
            displayColors: false,
            callbacks: {
              title: (items) => {
                if (!items || !items.length) return '';
                return fmtTime(Number(items[0].label));
              },
              label: (item) => {
                const v = item.parsed.y;
                if (v == null) return 'Timeout';
                return 'Latenz: ' + v.toFixed(1) + ' ms';
              },
            },
          },
          annotation: {
            annotations: [
              ...(outageMarkers || []).map((m) => ({
                type: 'box', xMin: m.start, xMax: m.end,
                backgroundColor: 'rgba(239,68,68,0.15)', borderWidth: 0,
              })),
              ...(spikeMarkers || []).map((m) => ({
                type: 'box', xMin: m.start, xMax: m.end,
                backgroundColor: 'rgba(245,158,11,0.10)', borderWidth: 0,
              })),
            ],
          },
        },
        scales: {
          x: { display: false },
          y: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1f2937' } },
        },
      },
    });
  }

  function histogramChart(canvas, bins, edgesMs) {
    const C = _maybeChart();
    if (!C || !canvas) return null;
    return new C(canvas, {
      type: 'bar',
      data: {
        labels: (edgesMs || bins.map((_, i) => i)).slice(0, bins.length),
        datasets: [
          {
            data: bins,
            backgroundColor: bins.map((_, i) => (i < 16 ? '#3b82f6' : i < 20 ? '#fbbf24' : '#ef4444')),
            borderWidth: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { display: false }, tooltip: { enabled: true } },
        scales: {
          x: { ticks: { color: '#6b7280', font: { size: 9 } }, grid: { display: false } },
          y: { ticks: { color: '#6b7280', font: { size: 9 } }, grid: { color: '#1f2937' } },
        },
      },
    });
  }

  function streamThroughputChart(canvas, series, drops) {
    const C = _maybeChart();
    if (!C || !canvas) return null;
    // Stable y-axis: pin the bottom at 0 and leave headroom above the peak.
    // Without an explicit min, Chart.js auto-fits the axis to the data's
    // min/max, which zooms into the 1-2 KB measurement ripple and makes a
    // steady ~20 KB/s stream look like a comb of outages.
    const vals = series.map((p) => p.kbps || 0);
    const peak = vals.length ? Math.max(...vals) : 0;
    // Round up to the next 25 KB/s -> at the default 20 KB/s stream the scale is
    // a fixed 0-25, so a dip to e.g. 18 reads as a small wiggle near the top
    // rather than looking like it crashed to zero.
    const yMax = Math.max(25, Math.ceil(peak / 25) * 25);
    return new C(canvas, {
      type: 'line',
      data: {
        labels: series.map((p) => p.ts || ''),
        datasets: [
          {
            label: 'kbps',
            data: series.map((p) => p.kbps),
            borderColor: '#3b82f6',
            backgroundColor: 'rgba(59,130,246,0.10)',
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.3,
            fill: true,
          },
          ...(drops && drops.length
            ? [{
                label: 'drops',
                data: drops.map((d) => d.kbps || 0),
                borderColor: '#ef4444',
                pointRadius: 3,
                showLine: false,
              }]
            : []),
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false },
          y: {
            min: 0,
            max: yMax,
            ticks: { color: '#6b7280', font: { size: 9 } },
            grid: { color: '#1f2937' },
          },
        },
      },
    });
  }

  function rssiChart(canvas, dataPoints) {
    const C = _maybeChart();
    if (!C || !canvas) return null;
    const fmtTime = (ts) => {
      if (!ts) return '';
      const d = new Date(ts);
      const pad = (n) => String(n).padStart(2, '0');
      return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    };
    const pts = dataPoints || [];
    return new C(canvas, {
      type: 'line',
      data: {
        labels: pts.map((p) => p.ts_ms || p.ts || p.x || ''),
        datasets: [
          {
            label: 'RSSI',
            data: pts.map((p) => (p.rssi != null ? p.rssi : null)),
            borderColor: '#fbbf24',
            backgroundColor: 'rgba(251,191,36,0.10)',
            borderWidth: 1.5,
            tension: 0.2,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: '#fbbf24',
            pointHoverBorderColor: '#ffffff',
            pointHoverBorderWidth: 2,
            fill: true,
            spanGaps: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false, axis: 'x' },
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: true,
            mode: 'index',
            intersect: false,
            backgroundColor: 'rgba(15,22,32,0.95)',
            titleColor: '#ffffff',
            bodyColor: '#e5e7eb',
            borderColor: '#374151',
            borderWidth: 1,
            padding: 10,
            titleFont: { size: 13, weight: '600' },
            bodyFont: { size: 13 },
            displayColors: false,
            callbacks: {
              title: (items) => {
                if (!items || !items.length) return '';
                return fmtTime(Number(items[0].label));
              },
              label: (item) => {
                const v = item.parsed.y;
                if (v == null) return 'kein Sample';
                return 'RSSI: ' + v + ' dBm';
              },
            },
          },
        },
        scales: {
          x: { display: false },
          y: {
            min: -100, max: -30,
            ticks: { color: '#6b7280', font: { size: 10 }, callback: (v) => v + ' dBm' },
            grid: { color: '#1f2937' },
          },
        },
      },
    });
  }

  global.PingWatchCharts = { latencyChart, histogramChart, streamThroughputChart, rssiChart };
})(window);
