const pageTabs = Array.from(document.querySelectorAll(".page-tab"));
const pages = {
  dashboard: document.getElementById("page-dashboard"),
  history: document.getElementById("page-history"),
  alerts: document.getElementById("page-alerts"),
};

const metricCards = document.getElementById("metric-cards");
const connectionStatus = document.getElementById("connection-status");
const lastUpdated = document.getElementById("last-updated");
const connectionDetails = document.getElementById("connection-details");
const overviewGrid = document.getElementById("overview-grid");
const alertSettingsGrid = document.getElementById("alert-settings-grid");
const alertsList = document.getElementById("alerts-list");

const chart = document.getElementById("history-chart");
const chartTooltip = document.getElementById("chart-tooltip");
const seriesPicker = document.getElementById("series-picker");
const chartLegend = document.getElementById("chart-legend");
const zoomInButton = document.getElementById("zoom-in");
const zoomOutButton = document.getElementById("zoom-out");
const zoomStatus = document.getElementById("zoom-status");
const timelineSlider = document.getElementById("timeline-slider");
const deficitToggle = document.getElementById("deficit-toggle");

const metrics = [
  ["Load", "output_active_power_w", "W"],
  ["Solar", "pv_input_power_w", "W"],
  ["Battery Voltage", "battery_voltage_v", "V"],
  ["Capacity", "battery_capacity_percent", "%"],
  ["Load Level", "load_percent", "%"],
  ["Solar Coverage", "solar_coverage_percent", "%"],
  ["Net Load After Solar", "net_load_after_solar_w", "W"],
];

const chartSeries = [
  { key: "output_active_power_w", label: "Load watts", unit: "W", colorVar: "var(--load)", swatch: "swatch-load", axis: "power", checked: true },
  { key: "pv_input_power_w", label: "Solar watts", unit: "W", colorVar: "var(--solar)", swatch: "swatch-solar", axis: "power", checked: true },
  { key: "battery_voltage_v", label: "Battery voltage", unit: "V", colorVar: "var(--voltage)", swatch: "swatch-voltage", axis: "voltage", checked: false },
  { key: "battery_capacity_percent", label: "Battery capacity", unit: "%", colorVar: "var(--battery)", swatch: "swatch-battery", axis: "percent", checked: true },
  { key: "load_percent", label: "Load level", unit: "%", colorVar: "var(--level)", swatch: "swatch-level", axis: "percent", checked: true },
];

const state = {
  page: "dashboard",
  livePayload: null,
  historyPoints: [],
  alertsPayload: null,
  currentLiveSample: {},
  visibleSeries: new Set(chartSeries.filter((series) => series.checked).map((series) => series.key)),
  viewportHours: 24,
  viewportEndFraction: 1,
  showSolarDeficit: true,
  zoomSteps: [1, 3, 6, 12, 24, 72, 168],
  chartPointer: {
    locked: false,
    x: null,
  },
};

function formatNumber(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  const rounded = Math.abs(value) >= 100 ? Math.round(value) : Number(value).toFixed(1);
  return `${rounded}${unit ? ` ${unit}` : ""}`;
}

function enrichSample(sample) {
  if (!sample) return {};
  const load = Number(sample.output_active_power_w);
  const solar = Number(sample.pv_input_power_w);
  const solarCoverage = load > 0 && Number.isFinite(solar)
    ? Math.min(999, (solar / load) * 100)
    : null;
  const netLoad = Number.isFinite(load) && Number.isFinite(solar)
    ? load - solar
    : null;

  return {
    ...sample,
    solar_coverage_percent: Number.isFinite(solarCoverage) ? solarCoverage : null,
    net_load_after_solar_w: Number.isFinite(netLoad) ? netLoad : null,
  };
}

function setActivePage(page) {
  state.page = page;
  pageTabs.forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.page === page);
  });
  Object.entries(pages).forEach(([key, element]) => {
    element.classList.toggle("active", key === page);
  });
}

function renderCards(sample) {
  const enriched = enrichSample(sample);
  metricCards.innerHTML = "";
  metrics.forEach(([label, key, unit]) => {
    const card = document.createElement("article");
    card.className = "metric-card";
    card.innerHTML = `
      <div class="label">${label}</div>
      <div class="value">${formatNumber(enriched[key], unit)}</div>
      <div class="sub">${key.replaceAll("_", " ")}</div>
    `;
    metricCards.appendChild(card);
  });
}

function renderConnection(payload) {
  const ok = Boolean(payload?.sample) && !payload?.last_error;
  connectionStatus.textContent = ok ? "Receiving inverter data" : "Waiting on inverter";
  connectionStatus.className = `status-pill${ok ? "" : " warn"}`;
  lastUpdated.textContent = payload?.last_success_at
    ? `Last sample: ${new Date(payload.last_success_at).toLocaleString()}`
    : "Waiting for first sample";

  const details = [
    ["Serial Port", payload?.settings?.serial_port || "--"],
    ["Poll Interval", payload?.settings?.poll_seconds ? `${payload.settings.poll_seconds} sec` : "--"],
    ["Mock Mode", payload?.settings?.mock_mode ? "On" : "Off"],
    ["Last Error", payload?.last_error || "None"],
  ];

  connectionDetails.innerHTML = details.map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join("");
}

function buildOverview(sample, historyPoints) {
  const enriched = enrichSample(sample);
  const recentPoints = historyPoints.slice(-288);
  const validLoadPoints = recentPoints.filter((point) => point.output_active_power_w !== null && point.output_active_power_w !== undefined);
  const validSolarPoints = recentPoints.filter((point) => point.pv_input_power_w !== null && point.pv_input_power_w !== undefined);
  const validCapacityPoints = recentPoints.filter((point) => point.battery_capacity_percent !== null && point.battery_capacity_percent !== undefined);
  const deficitPoints = recentPoints.filter((point) =>
    point.output_active_power_w !== null &&
    point.pv_input_power_w !== null &&
    point.output_active_power_w > point.pv_input_power_w
  );

  const batteryContribution = enriched.output_active_power_w > 0 && enriched.net_load_after_solar_w !== null
    ? Math.min(100, Math.max(0, (Math.max(0, enriched.net_load_after_solar_w) / enriched.output_active_power_w) * 100))
    : null;
  const batteryPowerEstimate = enriched.battery_voltage_v && enriched.battery_discharge_current_a
    ? enriched.battery_voltage_v * enriched.battery_discharge_current_a
    : null;
  const batteryStatus = batteryPowerEstimate !== null && batteryPowerEstimate > 80
    ? "Discharging"
    : enriched.battery_charging_current_a && enriched.battery_charging_current_a > 1
      ? "Charging"
      : "Stable";
  const peakLoad = validLoadPoints.length ? Math.max(...validLoadPoints.map((point) => point.output_active_power_w)) : null;
  const peakSolar = validSolarPoints.length ? Math.max(...validSolarPoints.map((point) => point.pv_input_power_w)) : null;
  const averageSolarCoverage = validLoadPoints.length
    ? recentPoints.reduce((sum, point) => {
      if (!point.output_active_power_w || point.pv_input_power_w === null || point.pv_input_power_w === undefined) return sum;
      return sum + Math.min(100, (point.pv_input_power_w / point.output_active_power_w) * 100);
    }, 0) / validLoadPoints.length
    : null;
  const solarDeficitTime = recentPoints.length ? (deficitPoints.length / recentPoints.length) * 100 : null;
  const batteryTrend = validCapacityPoints.length > 4
    ? validCapacityPoints[validCapacityPoints.length - 1].battery_capacity_percent - validCapacityPoints[0].battery_capacity_percent
    : null;
  const batteryTrendText = batteryTrend === null
    ? "Not enough history yet"
    : batteryTrend > 1
      ? "Battery reserve has climbed in this window"
      : batteryTrend < -1
        ? "Battery reserve has fallen in this window"
        : "Battery reserve has stayed mostly flat";

  return [
    ["Solar Coverage Now", formatNumber(enriched.solar_coverage_percent, "%"), "Current load covered directly by solar"],
    ["Battery Contribution Now", formatNumber(batteryContribution, "%"), "Share of the present load likely supplied by battery"],
    ["Battery Status", batteryStatus, batteryPowerEstimate !== null ? `Estimated battery output ${formatNumber(batteryPowerEstimate, "W")}` : "Waiting for enough battery data"],
    ["Peak Load", formatNumber(peakLoad, "W"), "Highest load in the recent history window"],
    ["Peak Solar", formatNumber(peakSolar, "W"), "Highest solar input in the recent history window"],
    ["Solar Deficit Time", formatNumber(solarDeficitTime, "%"), "How often load has been above solar recently"],
    ["Average Solar Coverage", formatNumber(averageSolarCoverage, "%"), "Average load coverage across recent samples"],
    ["Battery Trend", batteryTrend === null ? "--" : `${batteryTrend > 0 ? "+" : ""}${batteryTrend.toFixed(1)}%`, batteryTrendText],
  ];
}

function renderOverview() {
  const cards = buildOverview(state.currentLiveSample, state.historyPoints);
  overviewGrid.innerHTML = cards.map(([label, value, sub]) => `
    <article class="overview-card">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      <div class="sub">${sub}</div>
    </article>
  `).join("");
}

function renderAlertSettings() {
  const settings = state.alertsPayload?.settings || {};
  const cards = [
    ["Low Battery Threshold", formatNumber(settings.low_battery_percent, "%"), "Creates a warning when battery capacity drops under this level."],
    ["High Load Threshold", formatNumber(settings.high_load_watts, "W"), "Creates a warning when load crosses this wattage."],
    ["Alert Cooldown", settings.alert_cooldown_minutes ? `${settings.alert_cooldown_minutes} min` : "--", "Prevents duplicate notifications from firing too often."],
    ["Push Delivery", settings.ntfy_enabled ? "Enabled" : "Not configured", "Set POWER_MONITOR_NTFY_TOPIC_URL on the Pi to receive push notifications."],
  ];
  alertSettingsGrid.innerHTML = cards.map(([label, value, sub]) => `
    <article class="overview-card">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      <div class="sub">${sub}</div>
    </article>
  `).join("");
}

function renderAlerts() {
  renderAlertSettings();
  const alerts = state.alertsPayload?.alerts || [];
  if (!alerts.length) {
    alertsList.innerHTML = `<div class="empty-state">No alerts recorded yet. Once a threshold is crossed or the inverter stops responding, alerts will appear here.</div>`;
    return;
  }
  alertsList.innerHTML = alerts.map((alert) => `
    <article class="alert-card ${alert.level}">
      <div class="topline">
        <div class="title">${alert.title}</div>
        <div class="timestamp">${new Date(alert.ts_utc).toLocaleString()}</div>
      </div>
      <div class="message">${alert.message}</div>
      <div class="meta">
        <span>${alert.level.toUpperCase()}</span>
        <span> · </span>
        <span>${alert.delivered ? "Push sent" : "Stored locally"}</span>
      </div>
    </article>
  `).join("");
}

function buildSeriesControls() {
  seriesPicker.innerHTML = chartSeries.map((series) => `
    <label class="series-option">
      <input type="checkbox" data-series-key="${series.key}" ${state.visibleSeries.has(series.key) ? "checked" : ""} />
      <i class="swatch ${series.swatch}"></i>
      <span>${series.label}</span>
    </label>
  `).join("");

  chartLegend.innerHTML = chartSeries
    .filter((series) => state.visibleSeries.has(series.key))
    .map((series) => `<span><i class="swatch ${series.swatch}"></i>${series.label}</span>`)
    .join("");

  seriesPicker.querySelectorAll("input[type='checkbox']").forEach((checkbox) => {
    checkbox.addEventListener("change", (event) => {
      const key = event.target.dataset.seriesKey;
      if (!key) return;
      if (event.target.checked) {
        state.visibleSeries.add(key);
      } else if (state.visibleSeries.size > 1) {
        state.visibleSeries.delete(key);
      } else {
        event.target.checked = true;
      }
      buildSeriesControls();
      renderChart();
    });
  });
}

function computeGapThresholdMs(points) {
  if (points.length < 3) return 15 * 60 * 1000;
  const gaps = [];
  for (let index = 1; index < points.length; index += 1) {
    const gap = new Date(points[index].ts_utc).getTime() - new Date(points[index - 1].ts_utc).getTime();
    if (gap > 0) gaps.push(gap);
  }
  if (!gaps.length) return 15 * 60 * 1000;
  gaps.sort((a, b) => a - b);
  return Math.max(gaps[Math.floor(gaps.length / 2)] * 4, 15 * 60 * 1000);
}

function buildSmoothSegmentPath(segment) {
  if (!segment.length) return "";
  if (segment.length === 1) {
    return `M${segment[0].x.toFixed(1)},${segment[0].y.toFixed(1)}`;
  }

  const smoothingFactor = 4;
  let path = `M${segment[0].x.toFixed(1)},${segment[0].y.toFixed(1)}`;
  for (let index = 0; index < segment.length - 1; index += 1) {
    const p0 = segment[index - 1] || segment[index];
    const p1 = segment[index];
    const p2 = segment[index + 1];
    const p3 = segment[index + 2] || p2;
    const cp1x = p1.x + (p2.x - p0.x) / smoothingFactor;
    const cp1y = p1.y + (p2.y - p0.y) / smoothingFactor;
    const cp2x = p2.x - (p3.x - p1.x) / smoothingFactor;
    const cp2y = p2.y - (p3.y - p1.y) / smoothingFactor;
    path += ` C${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`;
  }
  return path;
}

function pathDataForSeries(points, key, yMin, yMax, width, height, pad, gapThresholdMs) {
  const values = points.map((point) => point[key]).filter((value) => value !== null && value !== undefined);
  if (!values.length) return "";
  const minTs = new Date(points[0].ts_utc).getTime();
  const maxTs = new Date(points[points.length - 1].ts_utc).getTime();
  const rangeTs = Math.max(1, maxTs - minTs);
  const rangeY = Math.max(1, yMax - yMin);
  let previousTs = null;
  let currentSegment = [];
  const segments = [];

  points.forEach((point) => {
    if (point[key] === null || point[key] === undefined) {
      previousTs = null;
      if (currentSegment.length) segments.push(currentSegment);
      currentSegment = [];
      return;
    }
    const pointTs = new Date(point.ts_utc).getTime();
    const x = pad + ((pointTs - minTs) / rangeTs) * (width - pad * 2);
    const y = height - pad - ((point[key] - yMin) / rangeY) * (height - pad * 2);
    if (previousTs === null || pointTs - previousTs > gapThresholdMs) {
      if (currentSegment.length) segments.push(currentSegment);
      currentSegment = [];
    }
    currentSegment.push({ x, y });
    previousTs = pointTs;
  });

  if (currentSegment.length) segments.push(currentSegment);
  return segments.map((segment) => buildSmoothSegmentPath(segment)).join(" ");
}

function buildDeficitOverlay(points, width, height, pad, gapThresholdMs) {
  if (!state.showSolarDeficit || points.length < 2) return "";
  const minTs = new Date(points[0].ts_utc).getTime();
  const maxTs = new Date(points[points.length - 1].ts_utc).getTime();
  const rangeTs = Math.max(1, maxTs - minTs);
  const overlays = [];

  for (let index = 0; index < points.length - 1; index += 1) {
    const current = points[index];
    const next = points[index + 1];
    const currentTs = new Date(current.ts_utc).getTime();
    const nextTs = new Date(next.ts_utc).getTime();
    const hasGap = nextTs - currentTs > gapThresholdMs;
    const hasValues = [current.output_active_power_w, current.pv_input_power_w, next.output_active_power_w, next.pv_input_power_w]
      .every((value) => value !== null && value !== undefined);
    if (hasGap || !hasValues) continue;
    if (!(current.output_active_power_w > current.pv_input_power_w || next.output_active_power_w > next.pv_input_power_w)) continue;
    const x1 = pad + ((currentTs - minTs) / rangeTs) * (width - pad * 2);
    const x2 = pad + ((nextTs - minTs) / rangeTs) * (width - pad * 2);
    overlays.push(`<rect x="${x1.toFixed(1)}" y="${pad}" width="${Math.max(1, x2 - x1).toFixed(1)}" height="${height - pad * 2}" fill="rgba(216,96,43,0.10)"></rect>`);
  }

  return overlays.join("");
}

function getViewportPoints(points) {
  if (!points.length) return points;
  const fullStart = new Date(points[0].ts_utc).getTime();
  const fullEnd = new Date(points[points.length - 1].ts_utc).getTime();
  const totalSpan = Math.max(1, fullEnd - fullStart);
  const viewportSpan = Math.min(totalSpan, state.viewportHours * 3600000);
  const maxStart = fullEnd - viewportSpan;
  const viewportStart = fullStart + (maxStart - fullStart) * state.viewportEndFraction;
  const viewportEnd = viewportStart + viewportSpan;
  return points.filter((point) => {
    const ts = new Date(point.ts_utc).getTime();
    return ts >= viewportStart && ts <= viewportEnd;
  });
}

function nearestPoint(points, targetTs) {
  let closest = points[0];
  let closestDistance = Math.abs(new Date(points[0].ts_utc).getTime() - targetTs);
  for (const point of points) {
    const distance = Math.abs(new Date(point.ts_utc).getTime() - targetTs);
    if (distance < closestDistance) {
      closest = point;
      closestDistance = distance;
    }
  }
  return closest;
}

function positionForPoint(point, minTs, rangeTs, minY, rangeY, width, height, pad, key) {
  if (point[key] === null || point[key] === undefined) return null;
  return {
    x: pad + ((new Date(point.ts_utc).getTime() - minTs) / rangeTs) * (width - pad * 2),
    y: height - pad - ((point[key] - minY) / rangeY) * (height - pad * 2),
  };
}

function tooltipHtml(point) {
  const rows = chartSeries
    .filter((series) => state.visibleSeries.has(series.key))
    .map((series) => `<div class="row"><span class="key">${series.label}</span><span>${formatNumber(point[series.key], series.unit)}</span></div>`)
    .join("");
  return `<div class="time">${new Date(point.ts_utc).toLocaleString()}</div>${rows}`;
}

function updateZoomStatus(totalSpanMs) {
  zoomStatus.textContent = state.viewportHours < 24
    ? `Viewing last ${state.viewportHours} hour${state.viewportHours === 1 ? "" : "s"}`
    : `Viewing last ${state.viewportHours / 24} day${state.viewportHours === 24 ? "" : "s"}`;
  zoomInButton.disabled = state.viewportHours === state.zoomSteps[0];
  zoomOutButton.disabled = state.viewportHours === state.zoomSteps[state.zoomSteps.length - 1];
  timelineSlider.disabled = totalSpanMs / 3600000 <= state.viewportHours;
}

function updateChartPointer(clientX) {
  const points = getViewportPoints(state.historyPoints);
  if (!points.length) return;

  const width = 1280;
  const height = 460;
  const pad = 64;
  const activeSeries = chartSeries.filter((series) => state.visibleSeries.has(series.key));
  const powerSeries = activeSeries.filter((series) => series.axis === "power");
  const percentSeries = activeSeries.filter((series) => series.axis === "percent");
  const voltageSeries = activeSeries.filter((series) => series.axis === "voltage");

  const powerValues = powerSeries.flatMap((series) => points.map((point) => point[series.key]).filter((value) => value !== null && value !== undefined));
  const percentValues = percentSeries.flatMap((series) => points.map((point) => point[series.key]).filter((value) => value !== null && value !== undefined));
  const voltageValues = voltageSeries.flatMap((series) => points.map((point) => point[series.key]).filter((value) => value !== null && value !== undefined));
  const powerMin = 0;
  const powerMax = powerValues.length ? Math.max(50, ...powerValues) * 1.1 : 100;
  const percentMin = 0;
  const percentMax = percentValues.length ? Math.max(100, ...percentValues.map((value) => Math.ceil(value / 10) * 10)) : 100;
  const voltageMin = voltageValues.length ? Math.floor(Math.min(...voltageValues) - 1) : 40;
  const voltageMax = voltageValues.length ? Math.ceil(Math.max(...voltageValues) + 1) : 60;

  const minTs = new Date(points[0].ts_utc).getTime();
  const maxTs = new Date(points[points.length - 1].ts_utc).getTime();
  const rangeTs = Math.max(1, maxTs - minTs);
  const powerRange = Math.max(1, powerMax - powerMin);
  const percentRange = Math.max(1, percentMax - percentMin);
  const voltageRange = Math.max(1, voltageMax - voltageMin);

  const bounds = chart.getBoundingClientRect();
  const relativeX = ((clientX - bounds.left) / bounds.width) * width;
  const ratio = Math.max(0, Math.min(1, (relativeX - pad) / (width - pad * 2)));
  const targetTs = minTs + ratio * rangeTs;
  const point = nearestPoint(points, targetTs);
  const x = pad + ((new Date(point.ts_utc).getTime() - minTs) / rangeTs) * (width - pad * 2);

  const cursorLine = document.getElementById("chart-cursor-line");
  const dots = Object.fromEntries(activeSeries.map((series) => [series.key, document.getElementById(`dot-${series.key}`)]));
  cursorLine.setAttribute("x1", x.toFixed(1));
  cursorLine.setAttribute("x2", x.toFixed(1));
  cursorLine.setAttribute("opacity", "1");

  activeSeries.forEach((series) => {
    const minY = series.axis === "power" ? powerMin : series.axis === "percent" ? percentMin : voltageMin;
    const rangeY = series.axis === "power" ? powerRange : series.axis === "percent" ? percentRange : voltageRange;
    const pos = positionForPoint(point, minTs, rangeTs, minY, rangeY, width, height, pad, series.key);
    if (pos) {
      dots[series.key].setAttribute("cx", pos.x.toFixed(1));
      dots[series.key].setAttribute("cy", pos.y.toFixed(1));
      dots[series.key].setAttribute("opacity", "1");
    } else {
      dots[series.key].setAttribute("opacity", "0");
    }
  });

  chartTooltip.innerHTML = tooltipHtml(point);
  chartTooltip.classList.remove("hidden");
  chartTooltip.classList.toggle("locked", state.chartPointer.locked);
  const tooltipLeft = Math.min(bounds.width - 240, Math.max(12, (x / width) * bounds.width + 12));
  chartTooltip.style.left = `${tooltipLeft}px`;
  state.chartPointer.x = clientX;
}

function clearChartPointer() {
  const cursorLine = document.getElementById("chart-cursor-line");
  if (cursorLine) cursorLine.setAttribute("opacity", "0");
  chart.querySelectorAll("circle[id^='dot-']").forEach((dot) => dot.setAttribute("opacity", "0"));
  chartTooltip.classList.add("hidden");
  chartTooltip.classList.remove("locked");
  state.chartPointer.x = null;
}

function renderChart() {
  const points = state.historyPoints;
  const width = 1280;
  const height = 460;
  const pad = 64;

  if (!points.length) {
    chart.innerHTML = "";
    clearChartPointer();
    return;
  }

  const totalSpanMs = Math.max(1, new Date(points[points.length - 1].ts_utc).getTime() - new Date(points[0].ts_utc).getTime());
  updateZoomStatus(totalSpanMs);
  const viewportPoints = getViewportPoints(points);
  if (!viewportPoints.length) {
    chart.innerHTML = "";
    clearChartPointer();
    return;
  }

  const activeSeries = chartSeries.filter((series) => state.visibleSeries.has(series.key));
  const powerSeries = activeSeries.filter((series) => series.axis === "power");
  const percentSeries = activeSeries.filter((series) => series.axis === "percent");
  const voltageSeries = activeSeries.filter((series) => series.axis === "voltage");

  const powerValues = powerSeries.flatMap((series) => viewportPoints.map((point) => point[series.key]).filter((value) => value !== null && value !== undefined));
  const percentValues = percentSeries.flatMap((series) => viewportPoints.map((point) => point[series.key]).filter((value) => value !== null && value !== undefined));
  const voltageValues = voltageSeries.flatMap((series) => viewportPoints.map((point) => point[series.key]).filter((value) => value !== null && value !== undefined));

  const powerMin = 0;
  const powerMax = powerValues.length ? Math.max(50, ...powerValues) * 1.1 : 100;
  const percentMin = 0;
  const percentMax = percentValues.length ? Math.max(100, ...percentValues.map((value) => Math.ceil(value / 10) * 10)) : 100;
  const voltageMin = voltageValues.length ? Math.floor(Math.min(...voltageValues) - 1) : 40;
  const voltageMax = voltageValues.length ? Math.ceil(Math.max(...voltageValues) + 1) : 60;
  const gapThresholdMs = computeGapThresholdMs(viewportPoints);
  const deficitOverlay = buildDeficitOverlay(viewportPoints, width, height, pad, gapThresholdMs);
  const horizontalGuides = [0.2, 0.4, 0.6, 0.8].map((fraction) => {
    const y = pad + (height - pad * 2) * fraction;
    return `<line x1="${pad}" y1="${y}" x2="${width - pad}" y2="${y}" stroke="rgba(31,35,33,0.10)" stroke-width="1" />`;
  }).join("");

  const axisValues = [0, 0.25, 0.5, 0.75, 1];
  const leftAxisLabels = powerSeries.length ? axisValues.map((fraction) => {
    const y = height - pad - fraction * (height - pad * 2);
    const watts = Math.round(powerMin + fraction * (powerMax - powerMin));
    return `<text x="${pad - 14}" y="${y + 6}" text-anchor="end" font-size="16" font-weight="700" fill="rgba(88,98,100,0.95)">${watts}W</text>`;
  }).join("") : "";
  const rightAxisLabels = percentSeries.length ? axisValues.map((fraction) => {
    const y = height - pad - fraction * (height - pad * 2);
    const percent = Math.round(percentMin + fraction * (percentMax - percentMin));
    return `<text x="${width - pad + 14}" y="${y + 6}" font-size="16" font-weight="700" fill="rgba(88,98,100,0.95)">${percent}%</text>`;
  }).join("") : "";
  const voltageAxisLabels = voltageSeries.length ? axisValues.map((fraction) => {
    const y = height - pad - fraction * (height - pad * 2);
    const volts = (voltageMin + fraction * (voltageMax - voltageMin)).toFixed(1);
    return `<text x="${pad + 18}" y="${y + 6}" font-size="15" font-weight="700" fill="rgba(21,122,110,0.95)">${volts}V</text>`;
  }).join("") : "";

  const polylines = activeSeries.map((series) => {
    const yMin = series.axis === "power" ? powerMin : series.axis === "percent" ? percentMin : voltageMin;
    const yMax = series.axis === "power" ? powerMax : series.axis === "percent" ? percentMax : voltageMax;
    return `<path fill="none" stroke="${series.colorVar}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" d="${pathDataForSeries(viewportPoints, series.key, yMin, yMax, width, height, pad, gapThresholdMs)}"></path>`;
  }).join("");

  const circles = activeSeries.map((series) => `<circle id="dot-${series.key}" r="5" fill="${series.colorVar}" opacity="0"></circle>`).join("");

  chart.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="transparent"></rect>
    ${horizontalGuides}
    ${deficitOverlay}
    ${leftAxisLabels}
    ${rightAxisLabels}
    ${voltageAxisLabels}
    ${polylines}
    <line id="chart-cursor-line" x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" stroke="rgba(31,35,33,0.20)" stroke-width="2" opacity="0"></line>
    ${circles}
  `;

  if (state.chartPointer.locked && state.chartPointer.x !== null) {
    updateChartPointer(state.chartPointer.x);
  } else {
    clearChartPointer();
  }
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return response.json();
}

async function refreshLive() {
  const payload = await fetchJson("/api/live");
  state.livePayload = payload;
  state.currentLiveSample = payload.sample || {};
  renderCards(state.currentLiveSample);
  renderConnection(payload);
  renderOverview();
}

async function refreshHistory() {
  const payload = await fetchJson("/api/history?hours=168");
  state.historyPoints = payload.points || [];
  renderChart();
  renderOverview();
}

async function refreshAlerts() {
  const payload = await fetchJson("/api/alerts");
  state.alertsPayload = payload;
  renderAlerts();
}

function stepZoom(direction) {
  const currentIndex = state.zoomSteps.indexOf(state.viewportHours);
  const nextIndex = Math.min(state.zoomSteps.length - 1, Math.max(0, currentIndex + direction));
  state.viewportHours = state.zoomSteps[nextIndex];
  renderChart();
}

function bindNavigation() {
  pageTabs.forEach((tab) => {
    tab.addEventListener("click", () => setActivePage(tab.dataset.page));
  });
}

function bindChartInteractions() {
  let lastTouchX = null;

  chart.addEventListener("mousemove", (event) => {
    if (state.chartPointer.locked) return;
    updateChartPointer(event.clientX);
  });

  chart.addEventListener("mouseleave", () => {
    if (!state.chartPointer.locked) clearChartPointer();
  });

  chart.addEventListener("click", (event) => {
    if (state.chartPointer.locked) {
      state.chartPointer.locked = false;
      clearChartPointer();
      return;
    }
    state.chartPointer.locked = true;
    updateChartPointer(event.clientX);
  });

  chart.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1) return;
    lastTouchX = event.touches[0].clientX;
    if (!state.chartPointer.locked) updateChartPointer(lastTouchX);
  }, { passive: true });

  chart.addEventListener("touchmove", (event) => {
    if (event.touches.length !== 1 || state.historyPoints.length < 2 || state.chartPointer.locked) return;
    const touchX = event.touches[0].clientX;
    if (lastTouchX === null) {
      lastTouchX = touchX;
      return;
    }
    const delta = lastTouchX - touchX;
    const nextValue = Math.max(0, Math.min(1000, Number(timelineSlider.value) + delta * 1.8));
    timelineSlider.value = String(nextValue);
    state.viewportEndFraction = nextValue / 1000;
    renderChart();
    lastTouchX = touchX;
  }, { passive: true });

  chart.addEventListener("touchend", () => {
    lastTouchX = null;
  }, { passive: true });
}

function bindControls() {
  zoomInButton.addEventListener("click", () => stepZoom(-1));
  zoomOutButton.addEventListener("click", () => stepZoom(1));
  timelineSlider.addEventListener("input", () => {
    state.viewportEndFraction = Number(timelineSlider.value) / 1000;
    renderChart();
  });
  deficitToggle.checked = state.showSolarDeficit;
  deficitToggle.addEventListener("change", () => {
    state.showSolarDeficit = deficitToggle.checked;
    renderChart();
  });
}

async function initialLoad() {
  await Promise.all([refreshLive(), refreshHistory(), refreshAlerts()]);
}

bindNavigation();
bindControls();
bindChartInteractions();
buildSeriesControls();
setActivePage(state.page);
initialLoad().catch((error) => {
  connectionStatus.textContent = "Dashboard offline";
  connectionStatus.className = "status-pill warn";
  lastUpdated.textContent = error.message;
});
setInterval(() => refreshLive().catch(() => {}), 3000);
setInterval(() => refreshHistory().catch(() => {}), 60000);
setInterval(() => refreshAlerts().catch(() => {}), 30000);
