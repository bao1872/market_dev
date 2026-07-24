/* ==========================================================================
   data-principles.js — 指标原理页专用脚本
   作用域：IIFE，仅在 #consensusSvg 存在时初始化（即 data.html）
   来源：ref/panji_indicator_principles_demo.html，移植为独立文件
   规则：默认不自动播放；不依赖外部 CDN；不污染全局
   ========================================================================== */
(function () {
  'use strict';

  // 仅在指标原理页运行
  if (!document.getElementById('consensusSvg')) return;

  var NS = 'http://www.w3.org/2000/svg';
  var q = function (id) { return document.getElementById(id); };
  var svgEl = function (tag, attrs, text) {
    var el = document.createElementNS(NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    if (text) el.textContent = text;
    return el;
  };
  var clamp = function (v, a, b) { return Math.max(a, Math.min(b, v)); };
  var fmt = function (v) { return Number(v).toFixed(2); };

  function addGrid(svg, x0, y0, w, h, rows, cols) {
    rows = rows || 6; cols = cols || 10;
    var g = svgEl('g', { opacity: '.9' });
    for (var i = 0; i <= rows; i++) g.appendChild(svgEl('line', { x1: x0, y1: y0 + i * h / rows, x2: x0 + w, y2: y0 + i * h / rows, class: 'axis-line' }));
    for (var j = 0; j <= cols; j++) g.appendChild(svgEl('line', { x1: x0 + j * w / cols, y1: y0, x2: x0 + j * w / cols, y2: y0 + h, class: 'axis-line' }));
    svg.appendChild(g);
    return g;
  }

  var bars = [
    { o: 98.2, h: 100.1, l: 97.5, c: 99.4, v: 210 }, { o: 99.4, h: 101.0, l: 98.8, c: 100.4, v: 245 }, { o: 100.4, h: 101.2, l: 99.1, c: 99.7, v: 225 },
    { o: 99.7, h: 101.5, l: 99.4, c: 101.0, v: 265 }, { o: 101.0, h: 102.1, l: 100.0, c: 100.5, v: 235 }, { o: 100.5, h: 102.4, l: 100.2, c: 101.9, v: 290 },
    { o: 102.0, h: 104.2, l: 101.5, c: 103.5, v: 355 }, { o: 103.5, h: 105.0, l: 102.8, c: 104.4, v: 410 }, { o: 104.4, h: 105.5, l: 103.1, c: 103.8, v: 385 },
    { o: 103.8, h: 106.2, l: 103.5, c: 105.6, v: 445 }, { o: 105.6, h: 106.6, l: 104.4, c: 105.0, v: 395 }, { o: 105.0, h: 107.2, l: 104.8, c: 106.7, v: 470 },
    { o: 106.8, h: 109.2, l: 106.3, c: 108.7, v: 565 }, { o: 108.7, h: 110.4, l: 108.1, c: 109.8, v: 625 }, { o: 109.8, h: 111.0, l: 108.9, c: 109.3, v: 590 },
    { o: 109.3, h: 111.7, l: 109.0, c: 111.0, v: 680 }, { o: 111.0, h: 112.2, l: 110.2, c: 111.6, v: 710 }, { o: 111.6, h: 113.1, l: 110.9, c: 112.7, v: 735 }
  ];

  function buildConsensus() {
    var svg = q('consensusSvg');
    var W = 1040, H = 555, plot = { x: 54, y: 36, w: 615, h: 390 }, vol = { x: 54, y: 442, w: 615, h: 75 }, profile = { x: 744, y: 36, w: 235, h: 390 };
    var minP = 96, maxP = 114, binsN = 36, binStep = (maxP - minP) / binsN;
    var y = function (p) { return plot.y + (maxP - p) / (maxP - minP) * plot.h; };
    var x = function (i) { return plot.x + 20 + i * (plot.w - 40) / (bars.length - 1); };
    addGrid(svg, plot.x, plot.y, plot.w, plot.h, 6, 9);
    svg.appendChild(svgEl('text', { x: plot.x, y: 21, class: 'chart-label' }, '时间序列：价格与成交量'));
    svg.appendChild(svgEl('text', { x: profile.x, y: 21, class: 'chart-label' }, '价格序列：累计成交分布'));
    svg.appendChild(svgEl('line', { x1: 708, y1: 22, x2: 708, y2: 520, stroke: '#263943', 'stroke-dasharray': '5 6' }));
    for (var i = 0; i <= 6; i++) svg.appendChild(svgEl('text', { x: plot.x - 10, y: plot.y + i * plot.h / 6 + 4, 'text-anchor': 'end', class: 'chart-small' }, fmt(maxP - i * (maxP - minP) / 6)));

    var candleG = svgEl('g', { id: 'cCandles', class: 'fade-layer' }), volG = svgEl('g', { id: 'cVolumes', class: 'fade-layer' });
    var maxV = Math.max.apply(null, bars.map(function (b) { return b.v; }));
    bars.forEach(function (b, i) {
      var xx = x(i), up = b.c >= b.o, color = up ? '#ff7770' : '#55e487';
      candleG.appendChild(svgEl('line', { x1: xx, y1: y(b.h), x2: xx, y2: y(b.l), stroke: color, 'stroke-width': '1.4' }));
      candleG.appendChild(svgEl('rect', { x: xx - 5, y: Math.min(y(b.o), y(b.c)), width: 10, height: Math.max(3, Math.abs(y(b.o) - y(b.c))), fill: up ? color : '#071117', stroke: color, 'stroke-width': '1.2', rx: '1' }));
      var vh = b.v / maxV * vol.h;
      volG.appendChild(svgEl('rect', { x: xx - 5, y: vol.y + vol.h - vh, width: 10, height: vh, fill: color, opacity: '.45', rx: '2' }));
    });
    svg.appendChild(candleG); svg.appendChild(volG);
    svg.appendChild(svgEl('text', { x: plot.x, y: 538, class: 'chart-small' }, '每根柱仍按时间排列，暂时看不出哪个价位累计成交最多'));

    var profileValues = new Array(binsN).fill(0);
    bars.forEach(function (b) {
      var typical = (b.h + b.l + b.c) / 3;
      var idx = []; var totalW = 0;
      for (var j = 0; j < binsN; j++) {
        var mid = minP + (j + .5) * binStep;
        if (mid >= b.l - binStep / 2 && mid <= b.h + binStep / 2) {
          var half = Math.max((b.h - b.l) / 2, .5); var weight = Math.max(.12, 1 - Math.abs(mid - typical) / (half + .35));
          idx.push([j, weight]); totalW += weight;
        }
      }
      idx.forEach(function (pair) { profileValues[pair[0]] += b.v * pair[1] / totalW; });
    });
    var maxProfile = Math.max.apply(null, profileValues), profileG = svgEl('g', { id: 'cProfile', class: 'fade-layer' });
    var barH = profile.h / binsN - 1;
    profileValues.forEach(function (v, j) {
      var yy = y(minP + (j + 1) * binStep) + .5;
      var width = v / maxProfile * profile.w;
      var rect = svgEl('rect', { x: profile.x, y: yy, width: 0, height: barH, fill: '#63aef8', opacity: '.64', rx: '2', class: 'profile-bar', 'data-width': width });
      profileG.appendChild(rect);
    });
    svg.appendChild(profileG);

    var flowG = svgEl('g', { id: 'cFlows', class: 'fade-layer' });
    bars.forEach(function (b, i) {
      if (i % 2 !== 0 && i !== bars.length - 1) return;
      var typical = (b.h + b.l + b.c) / 3, xx = x(i), yy = y(typical), j = clamp(Math.floor((typical - minP) / binStep), 0, binsN - 1);
      var endY = y(minP + (j + .5) * binStep);
      flowG.appendChild(svgEl('path', { d: 'M' + xx + ',' + yy + ' C' + (xx + 80) + ',' + yy + ' ' + (profile.x - 85) + ',' + endY + ' ' + profile.x + ',' + endY, class: 'flow-line' }));
    });
    svg.appendChild(flowG);

    // Weighted 3-means on price bins for a transparent teaching demonstration.
    var centers = [99.5, 104.7, 110.2];
    for (var iter = 0; iter < 12; iter++) {
      var sums = [0, 0, 0], weights = [0, 0, 0];
      profileValues.forEach(function (v, j) {
        var p = minP + (j + .5) * binStep; var k = 0;
        if (Math.abs(p - centers[1]) < Math.abs(p - centers[k])) k = 1;
        if (Math.abs(p - centers[2]) < Math.abs(p - centers[k])) k = 2;
        sums[k] += p * v; weights[k] += v;
      });
      centers = centers.map(function (c, k) { return weights[k] ? sums[k] / weights[k] : c; });
    }
    var assigned = [[], [], []];
    profileValues.forEach(function (v, j) {
      var p = minP + (j + .5) * binStep; var k = 0;
      if (Math.abs(p - centers[1]) < Math.abs(p - centers[k])) k = 1;
      if (Math.abs(p - centers[2]) < Math.abs(p - centers[k])) k = 2;
      assigned[k].push({ j: j, p: p, v: v });
    });
    var clusterStats = assigned.map(function (arr, k) {
      return {
        k: k, center: centers[k], volume: arr.reduce(function (s, d) { return s + d.v; }, 0),
        low: Math.min.apply(null, arr.filter(function (d) { return d.v > maxProfile * .04; }).map(function (d) { return d.p; })),
        high: Math.max.apply(null, arr.filter(function (d) { return d.v > maxProfile * .04; }).map(function (d) { return d.p; }))
      };
    });
    var totalVolume = clusterStats.reduce(function (s, d) { return s + d.volume; }, 0);
    var main = clusterStats.slice().sort(function (a, b) { return b.volume - a.volume; })[0];
    var colors = ['#63aef8', '#ad8cff', '#55e487'];
    var clusterG = svgEl('g', { id: 'cClusters', class: 'fade-layer' });
    clusterStats.forEach(function (s, k) {
      var yy = y(s.high + binStep / 2), hh = y(s.low - binStep / 2) - yy;
      clusterG.appendChild(svgEl('rect', { x: profile.x - 6, y: yy, width: profile.w + 12, height: hh, rx: '8', fill: colors[k], opacity: '.10', stroke: colors[k], 'stroke-width': '1.2', class: 'cluster-zone' }));
      clusterG.appendChild(svgEl('text', { x: profile.x + profile.w + 9, y: yy + hh / 2 + 4, fill: colors[k], 'font-size': '10' }, String.fromCharCode(65 + k) + ' ' + (s.volume / totalVolume * 100).toFixed(0) + '%'));
    });
    svg.appendChild(clusterG);

    var resultG = svgEl('g', { id: 'cResult', class: 'fade-layer' });
    var yTop = y(main.high + binStep / 2), yBottom = y(main.low - binStep / 2), yCenter = y(main.center);
    resultG.appendChild(svgEl('rect', { x: plot.x, y: yTop, width: plot.w + profile.w + 70, height: yBottom - yTop, fill: '#55e487', opacity: '.075' }));
    resultG.appendChild(svgEl('line', { x1: plot.x, y1: yCenter, x2: profile.x + profile.w, y2: yCenter, stroke: '#55e487', 'stroke-width': '2' }));
    resultG.appendChild(svgEl('line', { x1: profile.x - 8, y1: yTop, x2: profile.x + profile.w, y2: yTop, stroke: '#55e487', 'stroke-dasharray': '5 5' }));
    resultG.appendChild(svgEl('line', { x1: profile.x - 8, y1: yBottom, x2: profile.x + profile.w, y2: yBottom, stroke: '#55e487', 'stroke-dasharray': '5 5' }));
    resultG.appendChild(svgEl('text', { x: plot.x + 10, y: yTop + 18, fill: '#a9f3c0', 'font-size': '11', 'font-weight': '700' }, '主要成交聚集区'));
    resultG.appendChild(svgEl('text', { x: profile.x + 8, y: yCenter - 7, fill: '#b8f5cb', 'font-size': '10' }, '共识中心 ' + fmt(main.center)));
    svg.appendChild(resultG);

    var current = 112.7, currentG = svgEl('g', { id: 'cCurrent', class: 'fade-layer' }), cy = y(current);
    currentG.appendChild(svgEl('line', { x1: plot.x, y1: cy, x2: profile.x + profile.w, y2: cy, stroke: '#ff7770', 'stroke-width': '2.4' }));
    currentG.appendChild(svgEl('circle', { cx: profile.x + profile.w, cy: cy, r: '5', fill: '#ff7770' }));
    currentG.appendChild(svgEl('rect', { x: plot.x + 10, y: cy - 28, width: 158, height: 22, rx: '6', fill: '#2b1719', stroke: '#6b3335' }));
    currentG.appendChild(svgEl('text', { x: plot.x + 18, y: cy - 13, fill: '#ffaca7', 'font-size': '10' }, '当前价格 ' + fmt(current) + ' · 位于区间上方'));
    svg.appendChild(currentG);

    return { profileValues: profileValues, clusterStats: clusterStats, main: main, totalVolume: totalVolume, current: current, groups: { candleG: candleG, volG: volG, profileG: profileG, flowG: flowG, clusterG: clusterG, resultG: resultG, currentG: currentG } };
  }

  var cData = buildConsensus();
  var cSteps = [
    { k: '输入', bar: '读取原始价格与成交量', sub: '先明确输入：K线给出价格范围，成交量给出交易规模。', title: '先看每根K线发生了多少成交', summary: '横轴是时间，纵轴是价格。此时还不能直接看出哪些价格累计成交最多。', input: '一段已经发生的K线与成交量', process: '保持原始时间顺序，不提前给出共识结论', output: '价格路径和各时点成交规模', insight: '你应该先理解：筹码共识不是从一条价格线直接“猜”出来的，它同时使用价格位置和成交规模。' },
    { k: '转换', bar: '把成交量映射到价格位置', sub: '从时间轴转到价格轴，建立可追溯的数据关系。', title: '每根成交量沿其价格范围分配', summary: '动画用曲线把部分K线连接到右侧价格轴，说明同一根K线的成交会贡献到相应价格区间。', input: '单根K线的高低范围、代表价格与成交量', process: '教学示意：把成交量按价格范围分配到多个价格档', output: '每个价格档获得一部分成交贡献', insight: '关键变化：数据不再只回答“什么时候成交”，开始回答“在哪个价格附近成交”。' },
    { k: '中间结果', bar: '形成按价格排列的成交分布', sub: '横向柱越长，表示该价格附近累计成交越多。', title: '把所有K线贡献在同一价格轴上累加', summary: '右侧横向分布是计算的中间结果。它让用户直接看到成交密度随价格变化，而不是突然出现一个结论区。', input: '所有K线映射后的价格档成交贡献', process: '按价格档累计并归一化显示', output: '价格—累计成交量分布', insight: '只有先看到完整分布，后续的“聚集区”和“共识中心”才有可解释的来源。' },
    { k: '分组', bar: '识别多个成交聚集区域', sub: '相邻且成交密度接近的价格档被归为同一区域。', title: '先比较多个区域，不直接宣布唯一答案', summary: '动画把分布划分为 A、B、C 三个教学区域，并显示各区域占比。实际系统的组数与边界由数据决定。', input: '价格—成交量分布', process: '按价格接近程度与累计成交规模形成候选区域', output: '多个候选成交聚集区及其成交占比', insight: '“自动分组”必须可见：用户至少要知道系统比较了哪些区域，以及为什么某一区域更有代表性。' },
    { k: '指标输出', bar: '提取共识中心与上下边界', sub: '选择累计成交最有代表性的区域，再描述其中心和覆盖范围。', title: '共识不是一个点，而是中心加区间', summary: '绿色带表示主要成交聚集区；中心线是代表价格，上下虚线说明该区域的主要覆盖边界。', input: '多个候选区域及其累计成交规模', process: '选择代表性最强区域，并提取中心、下边界和上边界', output: '筹码共识中心与共识区间', insight: '区间比单一价格更诚实：成交聚集通常覆盖一段价格，而不是精确落在一个点。', metrics: true },
    { k: '状态解释', bar: '观察当前价格与共识区的关系', sub: '最后才加入当前价格，避免把结果误当成预测。', title: '相对位置描述当前状态，不直接产生信号', summary: '当前价格可以位于区间上方、内部或下方。这里演示位于上方，但这不等于未来必涨或已经出现买点。', input: '当前价格 + 共识中心与区间', process: '比较当前位置与上下边界', output: '上方 / 区间内部 / 下方的状态标签', insight: '指标回答“现在在哪里”，不回答“接下来一定怎么走”。', metrics: true }
  ];

  function renderConsensus(step) {
    var g = cData.groups;
    q('cCount').textContent = (step + 1) + ' / ' + cSteps.length;
    var d = cSteps[step];
    q('cKicker').textContent = d.k; q('cBarTitle').textContent = d.bar; q('cBarSub').textContent = d.sub;
    q('cTitle').textContent = d.title; q('cSummary').textContent = d.summary;
    q('cInput').textContent = d.input; q('cProcess').textContent = d.process; q('cOutput').textContent = d.output;
    q('cInsight').textContent = d.insight;
    var btns = q('cSteps').querySelectorAll('.ip-step-btn');
    for (var i = 0; i < btns.length; i++) btns[i].classList.toggle('active', i === step);
    g.candleG.style.opacity = '1'; g.volG.style.opacity = '1';
    g.flowG.style.opacity = step === 1 ? '1' : '0';
    g.profileG.style.opacity = step >= 1 ? '1' : '0';
    var bars2 = g.profileG.querySelectorAll('.profile-bar');
    for (var j = 0; j < bars2.length; j++) bars2[j].setAttribute('width', step >= 1 ? bars2[j].dataset.width : '0');
    g.clusterG.style.opacity = step >= 3 ? '1' : '0';
    g.resultG.style.opacity = step >= 4 ? '1' : '0';
    g.currentG.style.opacity = step >= 5 ? '1' : '0';
    q('cMetrics').innerHTML = d.metrics ? '<div class="ip-metric"><span>共识中心</span><strong>' + fmt(cData.main.center) + '</strong></div><div class="ip-metric"><span>主要区域占比</span><strong>' + (cData.main.volume / cData.totalVolume * 100).toFixed(1) + '%</strong></div><div class="ip-metric"><span>下边界</span><strong>' + fmt(cData.main.low) + '</strong></div><div class="ip-metric"><span>上边界</span><strong>' + fmt(cData.main.high) + '</strong></div>' : '';
  }

  var structureRaw = [100, 101, 100.6, 102, 101.7, 104, 103.5, 106, 105.4, 104.8, 105.2, 103.8, 104.2, 102, 103, 102.5, 105, 104.4, 107, 106.5, 110, 109.3, 108.6, 109, 107.5, 108.2, 105, 106.1, 105.5, 108, 107.4, 111, 110.4, 114, 113.2, 112.6, 113.0, 111.5, 112.1, 109, 110.2, 109.8, 112, 111.4, 115, 114.2, 118, 117.1, 116.2, 116.8, 115.1, 116.0, 113, 114.0, 113.6, 116, 115.5, 119, 118.2, 121.7];

  function localExtrema(arr) { var pts = []; for (var i = 1; i < arr.length - 1; i++) { if ((arr[i] > arr[i - 1] && arr[i] >= arr[i + 1]) || (arr[i] < arr[i - 1] && arr[i] <= arr[i + 1])) pts.push(i); } return pts; }
  function zigzag(arr, threshold) {
    threshold = threshold || .022;
    var out = [0]; var extremeIdx = 0, extreme = arr[0], dir = 0;
    for (var i = 1; i < arr.length; i++) {
      var p = arr[i];
      if (dir === 0) {
        if (p > extreme) { extreme = p; extremeIdx = i; } if (p < extreme) { extreme = p; extremeIdx = i; }
        var base = arr[0]; if ((p - base) / base >= threshold) { dir = 1; extreme = p; extremeIdx = i; } else if ((base - p) / base >= threshold) { dir = -1; extreme = p; extremeIdx = i; }
      } else if (dir === 1) {
        if (p >= extreme) { extreme = p; extremeIdx = i; } else if ((extreme - p) / extreme >= threshold) { out.push(extremeIdx); dir = -1; extreme = p; extremeIdx = i; }
      } else {
        if (p <= extreme) { extreme = p; extremeIdx = i; } else if ((p - extreme) / extreme >= threshold) { out.push(extremeIdx); dir = 1; extreme = p; extremeIdx = i; }
      }
    }
    if (out[out.length - 1] !== extremeIdx) out.push(extremeIdx);
    if (out[out.length - 1] !== arr.length - 1) out.push(arr.length - 1);
    var seen = {}; return out.filter(function (v) { if (seen[v]) return false; seen[v] = true; return true; }).sort(function (a, b) { return a - b; });
  }

  function buildStructure() {
    var svg = q('structureSvg'), plot = { x: 55, y: 42, w: 920, h: 430 }, minP = 98, maxP = 124;
    var x = function (i) { return plot.x + i * plot.w / (structureRaw.length - 1); }, y = function (p) { return plot.y + (maxP - p) / (maxP - minP) * plot.h; };
    addGrid(svg, plot.x, plot.y, plot.w, plot.h, 6, 10);
    svg.appendChild(svgEl('text', { x: plot.x, y: 24, class: 'chart-label' }, '价格路径：原始波动 → 候选转折 → 结构点 → 情景判断'));
    for (var i = 0; i <= 6; i++) svg.appendChild(svgEl('text', { x: plot.x - 10, y: plot.y + i * plot.h / 6 + 4, 'text-anchor': 'end', class: 'chart-small' }, fmt(maxP - i * (maxP - minP) / 6)));
    var rawPath = 'M' + structureRaw.map(function (p, i) { return x(i) + ',' + y(p); }).join(' L');
    var rawG = svgEl('g', { id: 'sRaw', class: 'fade-layer' }); rawG.appendChild(svgEl('path', { d: rawPath, fill: 'none', stroke: '#738793', 'stroke-width': '2', class: 'path-animate drawn' })); svg.appendChild(rawG);
    var candidates = localExtrema(structureRaw), candG = svgEl('g', { id: 'sCandidates', class: 'fade-layer' });
    candidates.forEach(function (i) { candG.appendChild(svgEl('circle', { cx: x(i), cy: y(structureRaw[i]), r: '3.4', fill: '#ad8cff', stroke: '#e0d3ff', 'stroke-width': '.8', class: 'structure-point' })); }); svg.appendChild(candG);
    var filtered = zigzag(structureRaw, .025), filterG = svgEl('g', { id: 'sFiltered', class: 'fade-layer' });
    var filtPath = 'M' + filtered.map(function (i) { return x(i) + ',' + y(structureRaw[i]); }).join(' L');
    var fp = svgEl('path', { d: filtPath, fill: 'none', stroke: '#55e487', 'stroke-width': '3', class: 'path-animate' }); filterG.appendChild(fp);
    filtered.forEach(function (i, pos) { filterG.appendChild(svgEl('circle', { cx: x(i), cy: y(structureRaw[i]), r: '5', fill: pos % 2 ? '#ff7770' : '#63aef8', stroke: '#071117', 'stroke-width': '2' })); }); svg.appendChild(filterG);

    var labelsG = svgEl('g', { id: 'sLabels', class: 'fade-layer' }); var prevHigh = null, prevLow = null; var rel = [];
    filtered.slice(1, -1).forEach(function (i, pos) {
      var p = structureRaw[i], prev = structureRaw[filtered[Math.max(0, pos)]], next = structureRaw[filtered[Math.min(filtered.length - 1, pos + 2)]];
      var high = p > prev && p > next; var label = '';
      if (high) { label = prevHigh === null ? 'H' : (p > prevHigh ? 'HH' : 'LH'); prevHigh = p; } else { label = prevLow === null ? 'L' : (p > prevLow ? 'HL' : 'LL'); prevLow = p; }
      rel.push({ i: i, p: p, high: high, label: label });
      labelsG.appendChild(svgEl('rect', { x: x(i) - 15, y: y(p) + (high ? -28 : 10), width: 30, height: 18, rx: '5', fill: high ? '#331b1d' : '#12263a', stroke: high ? '#7d3b3d' : '#315f84' }));
      labelsG.appendChild(svgEl('text', { x: x(i), y: y(p) + (high ? -15 : 23), 'text-anchor': 'middle', fill: high ? '#ffaaa5' : '#9bd0ff', 'font-size': '10', 'font-weight': '800' }, label));
    });
    var lastHigh = rel.slice().reverse().find(function (d) { return d.high; }), lastLow = rel.slice().reverse().find(function (d) { return !d.high; });
    if (lastHigh) labelsG.appendChild(svgEl('line', { x1: x(lastHigh.i), y1: y(lastHigh.p), x2: plot.x + plot.w, y2: y(lastHigh.p), stroke: '#ff7770', 'stroke-dasharray': '6 5' }));
    if (lastLow) labelsG.appendChild(svgEl('line', { x1: x(lastLow.i), y1: y(lastLow.p), x2: plot.x + plot.w, y2: y(lastLow.p), stroke: '#63aef8', 'stroke-dasharray': '6 5' }));
    svg.appendChild(labelsG);

    var scenarioG = svgEl('g', { id: 'sScenario', class: 'fade-layer' });
    scenarioG.appendChild(svgEl('rect', { x: 45, y: 34, width: 950, height: 456, rx: '12', fill: '#071117', stroke: '#22343e' }));
    scenarioG.appendChild(svgEl('line', { x1: 520, y1: 50, x2: 520, y2: 474, stroke: '#2a3d48' }));
    scenarioG.appendChild(svgEl('text', { x: 82, y: 76, fill: '#ffaaa5', 'font-size': '13', 'font-weight': '700' }, '情景 A｜突破前高：原结构延续'));
    scenarioG.appendChild(svgEl('text', { x: 557, y: 76, fill: '#9bd0ff', 'font-size': '13', 'font-weight': '700' }, '情景 B｜跌破关键前低：原结构失效'));
    function mini(offsetX, up) {
      var px = function (i) { return offsetX + 35 + i * 365 / (filtered.length - 1); }, py = function (p) { return 110 + (123 - p) / 14 * 300; };
      var base = filtered.slice(0, -1).map(function (i) { return structureRaw[i]; });
      var path = 'M' + base.map(function (p, i) { return px(i) + ',' + py(p); }).join(' L');
      scenarioG.appendChild(svgEl('path', { d: path, fill: 'none', stroke: '#55e487', 'stroke-width': '2.5' }));
      base.forEach(function (p, i) { scenarioG.appendChild(svgEl('circle', { cx: px(i), cy: py(p), r: '4', fill: i % 2 ? '#ff7770' : '#63aef8' })); });
      var startX = px(base.length - 1), startY = py(base[base.length - 1]);
      var future = up ? [116.0, 118.8, 117.6, 121.0] : [115.2, 112.2, 113.0, 110.5];
      var d = 'M' + startX + ',' + startY; future.forEach(function (p, i) { d += ' L' + (startX + 45 * (i + 1)) + ',' + py(p); });
      scenarioG.appendChild(svgEl('path', { d: d, fill: 'none', stroke: up ? '#ff7770' : '#63aef8', 'stroke-width': '3' }));
      var key = up ? (lastHigh ? lastHigh.p : 121) : (lastLow ? lastLow.p : 118.8);
      scenarioG.appendChild(svgEl('line', { x1: offsetX + 25, y1: py(key), x2: offsetX + 430, y2: py(key), stroke: up ? '#ff7770' : '#63aef8', 'stroke-dasharray': '6 5' }));
      scenarioG.appendChild(svgEl('text', { x: offsetX + 34, y: py(key) - 8, fill: up ? '#ffaaa5' : '#9bd0ff', 'font-size': '10' }, up ? '前高：向上突破才确认延续' : '关键前低：向下跌破才判定失效'));
    }
    mini(55, true); mini(530, false); svg.appendChild(scenarioG);
    return { groups: { rawG: rawG, candG: candG, filterG: filterG, labelsG: labelsG, scenarioG: scenarioG, fp: fp }, candidates: candidates, filtered: filtered, lastHigh: lastHigh, lastLow: lastLow };
  }
  var sData = buildStructure();
  var sSteps = [
    { k: '原始路径', bar: '保留完整原始价格路径', sub: '先展示噪声，避免一开始就把答案画好。', title: '先让用户看到真实波动有多杂乱', summary: '结构点不是肉眼随意挑选，也不是每个局部转折都有效。第一步必须保留全部波动作为对照。', input: '完整价格序列', process: '暂不平滑、不标结构结论', output: '包含大波段与小噪声的原始路径', insight: '你应该先理解：结构分析的价值，不是重新描一遍价格，而是压缩噪声后保留关键关系。' },
    { k: '候选点', bar: '标出所有局部高点和低点', sub: '先多选，再过滤；不能直接跳到最终结构点。', title: '紫色圆点是所有候选拐点', summary: '只要局部价格高于或低于相邻位置，就先标记为候选点。此时点很多，其中相当一部分只是噪声。', input: '完整价格路径', process: '识别局部高点和局部低点', output: '候选转折点集合', insight: '候选点不是最终结构点。把这两层混在一起，会让用户误以为结构点是任意挑选的。', metrics: true },
    { k: '过滤', bar: '过滤幅度过小或未确认的转折', sub: '被舍弃的点继续保留为灰色背景，形成清晰对照。', title: '绿色路径只连接保留下来的阶段转折', summary: '动画使用幅度阈值作教学示意：变化不足的候选点不会进入结构路径，从而把噪声压缩成少数关键节点。', input: '候选转折点集合', process: '按变化幅度与确认条件过滤', output: '阶段高点和阶段低点', insight: '必须让用户看到“谁被舍弃、谁被保留”，否则所谓过滤仍然只是黑箱。', metrics: true },
    { k: '结构关系', bar: '比较同类高点和低点的先后关系', sub: 'HH、HL、LH、LL比“阶段高点/低点”更有信息量。', title: '标注高点抬高与低点抬高', summary: 'HH表示后一个高点高于前一个高点，HL表示后一个低点高于前一个低点。虚线明确指出后续需要观察的关键水平。', input: '按时间排序的阶段高低点', process: '同类点之间进行前后比较，并保留最近关键高低点', output: 'HH / HL / LH / LL 与关键水平', insight: '结构判断的核心是关系，不是点的名称。用户必须知道突破或跌破的参照物是哪一个。', metrics: true },
    { k: '情景判断', bar: '把延续和失效拆成两个独立情景', sub: '避免在同一条未来路径上轮流画出互斥结论。', title: '突破前高与跌破关键前低必须分开看', summary: '左侧只演示向上突破前高，右侧只演示向下跌破关键前低。两个情景共享同一段历史，但代表不同后续条件。', input: '当前结构 + 最近关键高低点', process: '分别判断顺方向突破与反方向破坏', output: '结构延续，或原结构失效', insight: '结构失效不等于趋势必然反转；它只说明原先的高低点关系不能继续沿用。', metrics: true }
  ];

  function renderStructure(step) {
    var g = sData.groups, d = sSteps[step];
    q('sCount').textContent = (step + 1) + ' / ' + sSteps.length;
    q('sKicker').textContent = d.k; q('sBarTitle').textContent = d.bar; q('sBarSub').textContent = d.sub;
    q('sTitle').textContent = d.title; q('sSummary').textContent = d.summary;
    q('sInput').textContent = d.input; q('sProcess').textContent = d.process; q('sOutput').textContent = d.output;
    q('sInsight').textContent = d.insight;
    var btns = q('sSteps').querySelectorAll('.ip-step-btn');
    for (var i = 0; i < btns.length; i++) btns[i].classList.toggle('active', i === step);
    g.rawG.style.opacity = step === 4 ? '0' : '1';
    g.candG.style.opacity = step >= 1 && step < 4 ? (step === 2 || step === 3 ? '.18' : '1') : '0';
    g.filterG.style.opacity = step >= 2 && step < 4 ? '1' : '0';
    g.labelsG.style.opacity = step === 3 ? '1' : '0';
    g.scenarioG.style.opacity = step === 4 ? '1' : '0';
    g.fp.classList.toggle('drawn', step >= 2 && step < 4);
    q('sMetrics').innerHTML = d.metrics ? '<div class="ip-metric"><span>候选拐点</span><strong>' + sData.candidates.length + '</strong></div><div class="ip-metric"><span>保留结构点</span><strong>' + sData.filtered.length + '</strong></div><div class="ip-metric"><span>最近关键高点</span><strong>' + (sData.lastHigh ? fmt(sData.lastHigh.p) : '—') + '</strong></div><div class="ip-metric"><span>最近关键低点</span><strong>' + (sData.lastLow ? fmt(sData.lastLow.p) : '—') + '</strong></div>' : '';
  }

  // setupPlayer: 默认不自动播放（playing=false，仅 draw(0)）
  function setupPlayer(prefix, steps, render) {
    var index = 0, playing = false, timer = null; var play = q(prefix + 'Play');
    function draw(i, manual) { index = (i + steps.length) % steps.length; render(index); if (manual && playing) restart(); }
    function restart() { clearInterval(timer); if (playing) timer = setInterval(function () { draw(index + 1); }, 6200); }
    q(prefix + 'Prev').addEventListener('click', function () { draw(index - 1, true); });
    q(prefix + 'Next').addEventListener('click', function () { draw(index + 1, true); });
    play.addEventListener('click', function () {
      playing = !playing;
      play.innerHTML = playing ? '<svg viewBox="0 0 24 24" fill="none"><path d="M9 7v10M15 7v10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>' : '<svg viewBox="0 0 24 24"><path d="m9 7 8 5-8 5Z" fill="currentColor"/></svg>';
      play.setAttribute('aria-label', playing ? '暂停自动播放' : '开始自动播放');
      restart();
    });
    var stepBtns = q(prefix + 'Steps').querySelectorAll('.ip-step-btn');
    for (var i = 0; i < stepBtns.length; i++) stepBtns[i].addEventListener('click', function (ev) { draw(Number(ev.currentTarget.dataset.step), true); });
    draw(0);
    return function () { clearInterval(timer); };
  }
  var stopC = setupPlayer('c', cSteps, renderConsensus), stopS = setupPlayer('s', sSteps, renderStructure);
  document.addEventListener('visibilitychange', function () { if (document.hidden) { stopC(); stopS(); } });
})();
