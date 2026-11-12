/* Task output.csv viewer. Synthetic preview is explicit and never a fallback. */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const panel = $("lm-curves-content");
  if (!panel) return;
  let task = null, meta = null, data = null, chart = null, demo = false;
  let selected = new Set(), steps = new Set(), units = new Map(), slots = new Map();
  let generation = 0, dataRequest = 0, rawRequest = 0, rawOffset = 0, cursor = 0;
  const pending = new Set();
  const token = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const definitions = () => meta ? meta.columns.filter(d => d.id !== $("curve-time-column").value) : [];
  const active = () => definitions().filter(d => selected.has(d.id));
  const color = d => getComputedStyle(panel).getPropertyValue("--curve-series-" + ((slots.get(d.id) || 0) + 1)).trim();
  const timeUnit = () => $("curve-time-unit").value.trim();
  const unit = d => units.get(d.id) || "";
  const status = text => { $("curve-status").textContent = text; };
  const format = n => n == null ? "缺失 / 非数值" : Number(n.toPrecision(8)).toString();
  const timeLabel = t => format(t) + (timeUnit() ? " " + timeUnit() : "（时间单位未声明）");

  function base() {
    return "/api/v1/projects/" + task.projectId + "/tasks/" + encodeURIComponent(task.taskKey) + "/curves";
  }
  function params(extra) {
    const query = new URLSearchParams(extra || {});
    if (meta) query.set("revision", meta.revision);
    if ($("curve-encoding").value) query.set("encoding", $("curve-encoding").value);
    if ($("curve-delimiter").value) query.set("delimiter", $("curve-delimiter").value);
    return query;
  }
  function selection() {
    return { time: $("curve-time-column").value, columns: active().map(d => d.id).join(",") };
  }
  async function request(suffix, extra) {
    const epoch = generation, controller = new AbortController();
    pending.add(controller);
    try {
      const response = await fetch(base() + suffix + "?" + params(extra), { signal: controller.signal, credentials: "same-origin", cache: "no-store" });
      const body = await response.json();
      if (epoch !== generation) return null;
      if (!response.ok || !body.success) throw new Error((body.error || {}).message || "CSV 读取失败");
      return body.data;
    } catch (error) {
      if (epoch !== generation || error.name === "AbortError") return null;
      throw error;
    } finally { pending.delete(controller); }
  }
  function clear() {
    generation++; dataRequest++; rawRequest++;
    pending.forEach(c => c.abort()); pending.clear();
    if (chart) LMCharts.dispose(chart);
    chart = null; meta = null; data = null; cursor = 0; rawOffset = 0;
    selected = new Set(); steps = new Set(); units = new Map(); slots = new Map();
    $("curve-workspace").hidden = true;
    $("curve-download").hidden = true; $("curve-download").removeAttribute("href");
    $("curve-options").replaceChildren(); $("curve-values").replaceChildren(); $("curve-table").replaceChildren();
    $("curve-time-column").replaceChildren(new Option("请选择时间列", ""));
    $("curve-time-unit").value = ""; $("curve-search").value = "";
    $("curve-start").value = ""; $("curve-end").value = "";
    $("curve-readout-source").textContent = ""; $("curve-time-label").textContent = "—";
    $("curve-table-details").open = false;
    $("curve-summary").textContent = "";
  }
  function sourceLabel() {
    $("curve-source-label").textContent = demo ? "模拟数据 · 设计预览" : "任务产物 · output.csv";
    $("curve-source-note").textContent = demo
      ? "仅用于交互与样式评审，不代表本次任务结果。"
      : "读取当前任务的 CSV 原始产物；单位与数字量类型未声明时不会推断。";
    $("curve-demo").textContent = demo ? "返回任务 CSV" : "模拟预览";
    $("curve-time-column").disabled = demo;
  }
  async function load() {
    if (!task) return;
    clear(); sourceLabel();
    status(demo ? "加载明确标注的模拟预览…" : "正在读取当前任务 output.csv…");
    const epoch = generation;
    try {
      if (demo) {
        meta = { revision: "demo", encoding: "synthetic", delimiter: ",", suggested_time: "c0", columns: [
          {id:"c0", name:"Time", label:"Time", index:0},
          {id:"c1", name:"目标转速", label:"目标转速", numeric_preview:true, index:1},
          {id:"c2", name:"实际转速", label:"实际转速", numeric_preview:true, index:2},
          {id:"c3", name:"冷却液温度", label:"冷却液温度", numeric_preview:true, index:3},
        ] };
        units.set("c1", "rpm"); units.set("c2", "rpm"); units.set("c3", "°C"); steps.add("c1");
        $("curve-time-unit").value = "s";
      } else {
        // Raw download remains available even if decoding or parsing fails.
        $("curve-download").href = base() + "/download";
        $("curve-download").hidden = false;
        const result = await request("");
        if (!result || epoch !== generation) return;
        meta = result;
        $("curve-download").href = base() + "/download?" + params();
        $("curve-download").hidden = false;
      }
      meta.columns.forEach(d => $("curve-time-column").add(new Option(d.label, d.id)));
      $("curve-time-column").value = meta.suggested_time || "";
      definitions().filter(d => d.numeric_preview).slice(0, 3).forEach((d, i) => { selected.add(d.id); slots.set(d.id, i); });
      $("curve-workspace").hidden = false;
      options(); await refresh();
    } catch (error) { if (epoch === generation) status(error.message); }
  }
  function key(d) {
    const mark = document.createElement("span");
    mark.className = "curve-key" + (steps.has(d.id) ? " dashed" : "");
    mark.style.setProperty("--key-color", color(d)); mark.setAttribute("aria-hidden", "true");
    return mark;
  }
  function options() {
    const query = $("curve-search").value.trim().toLowerCase();
    $("curve-options").replaceChildren();
    definitions().filter(d => d.label.toLowerCase().includes(query)).forEach(d => {
      const box = document.createElement("div"); box.className = "curve-variable";
      const input = document.createElement("input"); input.type = "checkbox";
      input.checked = selected.has(d.id); input.setAttribute("aria-label", d.label);
      input.id = "curve-select-" + d.id;
      input.addEventListener("change", () => {
        if (input.checked && selected.size >= 8) { input.checked = false; status("最多同时显示 8 个变量。"); return; }
        if (input.checked) {
          const used = new Set(active().map(s => slots.get(s.id)));
          const previous = slots.get(d.id);
          const slot = previous !== undefined && !used.has(previous) ? previous : Array.from({length:8}, (_, i) => i).find(i => !used.has(i));
          slots.set(d.id, slot); selected.add(d.id);
        } else selected.delete(d.id);
        options(); refresh();
      });
      const text = document.createElement("div"); text.className = "curve-variable-text";
      const label = document.createElement("label"); label.htmlFor = input.id; label.className = "curve-variable-name";
      label.append(key(d), document.createTextNode(d.label));
      const note = document.createElement("code"); note.textContent = "列 " + (d.index + 1) + (d.numeric_preview ? " · 数值候选" : " · 文本 / 待确认");
      const settings = document.createElement("div"); settings.className = "curve-variable-settings";
      const unitInput = document.createElement("input"); unitInput.type = "text"; unitInput.maxLength = 24;
      unitInput.placeholder = "单位未声明"; unitInput.value = unit(d); unitInput.setAttribute("aria-label", d.label + " 单位（手动设置）");
      unitInput.addEventListener("change", () => { units.set(d.id, unitInput.value.trim()); draw(); });
      const style = document.createElement("select"); style.setAttribute("aria-label", d.label + " 显示方式");
      style.add(new Option("折线", "line")); style.add(new Option("阶梯（手动）", "step")); style.value = steps.has(d.id) ? "step" : "line";
      style.addEventListener("change", () => { if (style.value === "step") steps.add(d.id); else steps.delete(d.id); draw(); });
      settings.append(unitInput, style); text.append(label, note, settings); box.append(input, text); $("curve-options").append(box);
    });
    if (!$("curve-options").children.length) $("curve-options").textContent = "没有匹配变量";
    $("curve-count").textContent = selected.size + " / " + definitions().length;
  }
  function synthetic(all = false) {
    const ds = all ? meta.columns.filter(d => d.id !== "c0") : active();
    const points = Array.from({ length: 401 }, (_, i) => {
      const t = i / 20;
      const step = (at, delta) => t < at ? 0 : delta * (1 - Math.exp(-(t - at) * 2.4)) + delta * .12 * Math.exp(-(t - at) * 1.2) * Math.sin((t - at) * 5);
      const row = { c1: t < 3 ? 800 : t < 10 ? 2200 : t < 15 ? 1400 : 2600,
        c2: 800 + step(3, 1400) + step(10, -800) + step(15, 1200), c3: 76 + .72 * t + .8 * Math.sin(t / 2) };
      return { row: i + 1, t, values: ds.map(d => row[d.id]) };
    });
    return { points, domain:[0,20], range:[0,20], total_rows:401, range_rows:401, sampled:false, warnings:{} };
  }
  async function refresh(range) {
    if (!meta) return;
    const sequence = ++dataRequest, epoch = generation;
    data = null; $("curve-readout-source").textContent = ""; $("curve-values").replaceChildren();
    if (!$("curve-time-column").value || !active().length) {
      status(!$("curve-time-column").value ? "请选择时间列；保留原始数值，不自动转换时间单位。" : "请选择至少一个变量。");
      draw(); return;
    }
    $("curve-chart").hidden = true; status("正在验证时间列并读取有界时序数据…");
    try {
      const next = demo ? synthetic() : await request("/data", Object.assign(selection(), range || {}));
      if (!next || sequence !== dataRequest || epoch !== generation) return;
      data = next; cursor = 0;
      $("curve-start").value = data.range[0]; $("curve-end").value = data.range[1];
      const names = {blank_rows:"空记录",malformed_rows:"列数不符",invalid_time_rows:"无效时间",duplicate_time_rows:"重复时间",invalid_values:"无效数值"};
      const warnings = Object.entries(data.warnings).filter(([, n]) => n).map(([name,n]) => names[name] + " " + n).join("；");
      status((demo ? "模拟预览。" : "CSV：" + meta.encoding + "；时间与变量单位以原文件或手动设置为准。") + (warnings ? " 数据提示：" + warnings + "。" : ""));
      $("curve-summary").textContent = " · " + data.points.length + " / " + data.range_rows + " 个记录" + (data.sampled ? " · 保留极值抽样；可细化区间" : " · 当前区间未抽样");
      draw();
    } catch (error) {
      if (sequence === dataRequest && epoch === generation) { status(error.message); draw(); }
    }
  }
  function readout(index, original) {
    if (!data || !data.points.length) return;
    cursor = Math.max(0, Math.min(data.points.length - 1, index));
    const p = original || data.points[cursor];
    $("curve-time").value = cursor;
    $("curve-time-label").textContent = timeLabel(p.t);
    $("curve-time").setAttribute("aria-valuetext", timeLabel(p.t));
    $("curve-readout-source").textContent = original ? "原始 CSV · 记录 " + p.row + (p.duplicate_times > 1 ? " · 同时刻 " + p.duplicate_times + " 条，取首条" : "")
      : (demo ? "模拟值" : data.sampled ? "图中抽样值" : "原始记录（当前区间未抽样）") + " · 记录 " + p.row;
    $("curve-values").replaceChildren();
    active().forEach((d, i) => {
      const item = document.createElement("span"), number = document.createElement("strong");
      item.append(key(d), document.createTextNode(d.label));
      number.textContent = (original ? original.raw[d.index] : format(p.values[i])) + (unit(d) ? " " + unit(d) : "（无单位）");
      item.append(number); $("curve-values").append(item);
    });
  }
  function tooltip(params) {
    if (!data || !data.points.length) return "";
    const list = Array.isArray(params) ? params : [params];
    const index = list.length ? data.points.findIndex(p => p.row === list[0].data[2]) : cursor;
    const p = data.points[index < 0 ? cursor : index];
    readout(index < 0 ? cursor : index);
    const box = document.createElement("div"), title = document.createElement("strong"); box.className = "curve-tooltip";
    title.textContent = (demo ? "模拟数据" : data.sampled ? "图中抽样值" : "CSV 记录 " + p.row) + " · " + timeLabel(p.t); box.append(title);
    active().forEach((d, i) => {
      const row = document.createElement("p"), name = document.createElement("span"), number = document.createElement("strong");
      name.append(key(d), document.createTextNode(d.label)); number.textContent = format(p.values[i]) + (unit(d) ? " " + unit(d) : "");
      row.append(name, number); box.append(row);
    });
    return box;
  }
  function wireChart() {
    chart.on("datazoom", () => {
      const axis = chart.getOption().dataZoom[0];
      $("curve-start").value = axis.startValue;
      $("curve-end").value = axis.endValue;
    });
  }
  function draw() {
    const ds = active(), hasData = data && data.points.length && ds.length;
    $("curve-empty").hidden = Boolean(hasData);
    $("curve-chart").hidden = !hasData;
    $("curve-time").disabled = !hasData; $("curve-original").disabled = !hasData || demo;
    if (!hasData) { if (chart) chart.clear(); return; }
    if (panel.hidden) return;
    const tracks = [];
    ds.forEach(d => {
      // Unknown units never imply a shared scale. At most three lines per track.
      let track = $("curve-group").checked && unit(d) ? tracks.find(t => t.unit === unit(d) && t.items.length < 3) : null;
      if (!track) { track = { unit: unit(d), items: [] }; tracks.push(track); }
      track.items.push(d);
    });
    $("curve-chart").style.height = (tracks.length * 155 + 90) + "px";
    if (!chart) {
      chart = LMCharts.init($("curve-chart"));
      if (!chart) { status("图表组件不可用；可查看原始数据或下载 CSV。"); return; }
      wireChart();
    }
    const grids = tracks.map((_, i) => ({left:62,right:100,top:32+i*155,height:108}));
    const xAxes = tracks.map((_, i) => ({type:"value", gridIndex:i, min:data.range[0], max:data.range[1],
      axisLabel:{show:i===tracks.length-1, color:token("--text-3")},
      name:i===tracks.length-1 ? timeUnit() : "", nameTextStyle:{color:token("--text-2")},
      splitLine:{show:false}, axisPointer:{show:true,snap:true,label:{show:false}}}));
    const yAxes = tracks.map((t, i) => ({type:"value",gridIndex:i,name:t.unit || "单位未声明",scale:true,splitNumber:3,
      nameTextStyle:{color:token("--text-2")},axisLabel:{color:token("--text-3")},
      splitLine:{lineStyle:{color:token("--border"),width:1,type:"solid"}}}));
    chart.setOption({animation:false,textStyle:{fontFamily:token("--font"),color:token("--text-2")},
      grid:grids,xAxis:xAxes,yAxis:yAxes,
      axisPointer:{link:[{xAxisIndex:"all"}],lineStyle:{color:token("--text-3"),type:"dashed"}},
      tooltip:{trigger:"axis",confine:true,formatter:tooltip,backgroundColor:token("--surface"),borderColor:token("--border"),textStyle:{color:token("--text")}},
      dataZoom:[{type:"slider",xAxisIndex:tracks.map((_,i)=>i),bottom:14,height:22,start:0,end:100,borderColor:token("--border"),fillerColor:token("--brand-soft"),textStyle:{color:token("--text-3")}},
        {type:"inside",xAxisIndex:tracks.map((_,i)=>i),start:0,end:100}],
      series:ds.map((d,i) => {
        const axis = tracks.findIndex(t=>t.items.includes(d));
        return {id:d.id,name:d.label,type:"line",xAxisIndex:axis,yAxisIndex:axis,
          data:data.points.map(p=>[p.t,p.values[i],p.row]),showSymbol:data.points.length<=20,symbolSize:8,connectNulls:false,
          step:steps.has(d.id)?"end":false,
          lineStyle:{width:2,color:color(d),type:(slots.get(d.id)%2 || steps.has(d.id))?"dashed":"solid"},itemStyle:{color:color(d)},
          endLabel:{show:true,formatter:d.label,color:token("--text-2"),fontSize:11},labelLayout:{moveOverlap:"shiftY"}};
      })},true);
    chart.resize();
    $("curve-time").max = data.points.length - 1;
    readout(Math.min(cursor, data.points.length - 1));
  }
  async function rawPage() {
    if (!meta || !$("curve-table-details").open) return;
    const sequence = ++rawRequest, epoch = generation;
    try {
      const page = demo ? {rows:synthetic(true).points.slice(rawOffset,rawOffset+100).map(p=>({row:p.row,cells:[p.t,...p.values].map(String)})),has_more:rawOffset+100<401}
        : await request("/raw", {offset:rawOffset,count:100});
      if (!page || sequence !== rawRequest || epoch !== generation) return;
      const head = document.createElement("thead"), tr = document.createElement("tr"), body = document.createElement("tbody");
      ["记录",...meta.columns.map(d=>d.label)].forEach(name=>{const th=document.createElement("th");th.scope="col";th.textContent=name;tr.append(th);}); head.append(tr);
      page.rows.forEach(r=>{const row=document.createElement("tr");[r.row,...r.cells].forEach(cell=>{const td=document.createElement("td");td.textContent=String(cell);row.append(td);});body.append(row);});
      $("curve-table").replaceChildren(head,body);
      $("curve-page").textContent = "记录 " + (rawOffset+1) + "–" + (rawOffset+page.rows.length) + (demo?" · 模拟数据":" · 原始文本");
      $("curve-prev").disabled = rawOffset===0; $("curve-next").disabled=!page.has_more;
    } catch(error) { if(epoch===generation) status(error.message); }
  }
  function activate(curves) {
    [$("lm-result-tab"),$("lm-curves-tab")].forEach((tab,i)=>{const on=Boolean(i)===curves;tab.setAttribute("aria-selected",String(on));tab.tabIndex=on?0:-1;});
    $("lm-result-content").hidden=curves; panel.hidden=!curves;
    if(curves) { if(!meta) load(); else requestAnimationFrame(draw); }
  }
  [$("lm-result-tab"),$("lm-curves-tab")].forEach((tab,i,tabs)=>{
    tab.addEventListener("click",()=>activate(Boolean(i)));
    tab.addEventListener("keydown",event=>{
      if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
      event.preventDefault();const next=event.key==="Home"?0:event.key==="End"?1:1-i;activate(Boolean(next));tabs[next].focus();
    });
  });
  $("curve-demo").addEventListener("click",()=>{demo=!demo;load();});
  $("curve-reload").addEventListener("click",load);
  ["curve-encoding","curve-delimiter"].forEach(id=>$(id).addEventListener("change",()=>{demo=false;load();}));
  $("curve-search").addEventListener("input",options);
  $("curve-time-column").addEventListener("change",()=>{selected.delete($("curve-time-column").value);options();refresh();});
  $("curve-time-unit").addEventListener("change",draw);
  $("curve-group").addEventListener("change",draw);
  $("curve-reset").addEventListener("click",()=>refresh());
  $("curve-apply-range").addEventListener("click",()=>{
    const range={};if($("curve-start").value!=="") range.start=$("curve-start").value;if($("curve-end").value!=="") range.end=$("curve-end").value;refresh(range);
  });
  $("curve-time").addEventListener("input",()=>{readout(Number($("curve-time").value));if(chart) chart.dispatchAction({type:"showTip",seriesIndex:0,dataIndex:cursor});});
  $("curve-original").addEventListener("click",async()=>{
    if(!data || demo) return;
    const sequence=dataRequest,epoch=generation,at=data.points[cursor].t;
    try {const p=await request("/point",Object.assign(selection(),{at}));if(p && epoch===generation && sequence===dataRequest) readout(cursor,p);}
    catch(error){if(epoch===generation) status(error.message);}
  });
  $("curve-table-details").addEventListener("toggle",rawPage);
  $("curve-prev").addEventListener("click",()=>{rawOffset=Math.max(0,rawOffset-100);rawPage();});
  $("curve-next").addEventListener("click",()=>{rawOffset+=100;rawPage();});
  $("curve-chart").addEventListener("lm-chart-rebuilt",event=>{chart=event.detail.chart;wireChart();options();draw();});
  new ResizeObserver(()=>{if(chart&&!panel.hidden) chart.resize();}).observe($("curve-chart"));
  document.addEventListener("lm-task-open",event=>{
    clear();task=event.detail;demo=new URLSearchParams(location.search).get("curves")==="demo";
    $("curve-encoding").value="";$("curve-delimiter").value="";sourceLabel();
    status("尚未读取当前任务 CSV。");
    if(demo) activate(true); else if(!panel.hidden) load();
  });
  document.addEventListener("lm-task-close",()=>{clear();task=null;demo=false;sourceLabel();});
})();
