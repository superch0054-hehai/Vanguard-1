
var STAGES = [], rSince = 0, rTimer = null, rT0 = 0, rQ = {};
function rEsc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function rPaint(st){
  var h = "";
  (STAGES || []).forEach(function(lab, i){
    var c = (i < st.stage) ? "#2e7d32" : (i === st.stage ? "#1565c0" : "#bbb");
    h += "<span style='display:inline-block;padding:3px 9px;margin:2px;border-radius:11px;"
       + "font-size:12px;color:#fff;background:" + c + "'>" + (i+1) + ". " + rEsc(lab) + "</span>";
  });
  document.getElementById("rstages").innerHTML = h;
  var el = document.getElementById("rlog");
  if (st.lines && st.lines.length){
    st.lines.forEach(function(l){ el.textContent += l + "
"; });
    el.scrollTop = el.scrollHeight;
  }
  document.getElementById("rmsg").textContent = (st.state === "running")
      ? ("进行中　已等 " + Math.round((Date.now() - rT0) / 1000) + " 秒")
      : ("状态：" + st.state + (st.report ? "" : "（没有产出报告，看下面日志）"));
}
function rPoll(){
  fetch("/report/status?since=" + rSince).then(function(r){ return r.json(); }).then(function(st){
    rSince = st.n || rSince;
    if (!STAGES.length) STAGES = st.stages || [];
    rPaint(st);
    if (st.state === "running"){ rTimer = setTimeout(rPoll, 1000); return; }
    clearTimeout(rTimer);
    if (st.report){
      var u = "/report/file?a=" + encodeURIComponent(rQ.ce) + "&b=" + encodeURIComponent(rQ.cc)
              + "&c=" + encodeURIComponent(rQ.re) + "&d=" + encodeURIComponent(rQ.rc);
      document.getElementById("rlink").innerHTML =
        "<a href='" + u + "' target='_blank' style='font-weight:bold'>▶ 打开报告</a>";
    }
  }).catch(function(e){
    document.getElementById("rmsg").textContent = "查询状态失败：" + e;
    rTimer = setTimeout(rPoll, 3000);
  });
}
function rVal(id){ return document.getElementById(id).value; }
function startReport(){
  var m = document.getElementById("rmsg");
  rQ = {ce: rVal("r_ce"), cc: rVal("r_cc"), re: rVal("r_re"), rc: rVal("r_rc")};
  var all = [rQ.ce, rQ.cc, rQ.re, rQ.rc];
  if (all.some(function(x){ return !x; })){ m.textContent = "四个通道都要选"; return; }
  if (new Set(all).size !== 4){ m.textContent = "四个通道必须各不相同（现在有重复）"; return; }
  m.textContent = "已发送请求…";
  fetch("/report/start?a=" + encodeURIComponent(rQ.ce) + "&b=" + encodeURIComponent(rQ.cc)
        + "&c=" + encodeURIComponent(rQ.re) + "&d=" + encodeURIComponent(rQ.rc),
        {method:"POST"})
    .then(function(r){ return r.json(); })
    .then(function(d){
      m.textContent = d.msg;
      if (!d.ok) return;
      document.getElementById("rlog").textContent = "";
      document.getElementById("rlink").innerHTML = "";
      rSince = 0; rT0 = Date.now(); STAGES = []; rPoll();
    })
    .catch(function(e){ m.textContent = "起不来：" + e; });
}
