// Gemini Enterprise NotebookLM 协议探针
//
// 用法:
//   1. 用一个在 yuchizh-test-449107 里有 Gemini Enterprise license 的账号,
//      在 Chrome 打开 https://notebook.cloud.google.com/us/?project=260295163676
//      (region 用 us / eu / global 里你的 notebook 实际所在的那个)
//   2. 等页面完全加载出 notebook 列表
//   3. F12 -> Console -> 粘贴本文件全文 -> 回车
//   4. 把输出整段贴回来
//
// 它回答三个问题,合起来就是 notebooklm-py 能不能用:
//   A. GE 用的是不是同一套 LabsTailwindUi 前端?   -> 看 build label
//   B. batchexecute 端点在不在 /<region>/ 下?      -> 看 HTTP 状态
//   C. RPC ID 是不是同一批?                        -> 看响应体里有没有回显 wXbhsf + 数据

(async () => {
  const W = window.WIZ_global_data || {};
  const at = W["SNlM0e"];
  const sid = W["FdrFJe"];
  const bl = (document.documentElement.innerHTML.match(
    /boq_labs-tailwind-frontend_\d{8}\.\d{2}_p\d+/
  ) || [])[0];
  const region = location.pathname.split("/").filter(Boolean)[0];

  console.log("=== A. app shell ===");
  console.log("region         :", region);
  console.log("build label    :", bl || "(没找到 —— 说明不是 LabsTailwindUi 前端)");
  console.log("CSRF  SNlM0e   :", at ? "present" : "MISSING");
  console.log("session FdrFJe :", sid ? "present" : "MISSING");

  if (!at) {
    console.log("=> 页面没有暴露 CSRF token,到此为止。");
    return;
  }

  // LIST_NOTEBOOKS = ListRecentlyViewedProjects,只读,不会改任何东西
  const rpcid = "wXbhsf";
  const params = JSON.stringify([null, 1, null, [2]]);
  const fReq = JSON.stringify([[[rpcid, params, null, "generic"]]]);
  const qs = new URLSearchParams({
    rpcids: rpcid,
    "source-path": "/",
    "f.sid": sid || "",
    hl: "en",
    rt: "c",
  });
  const url = `/${region}/_/LabsTailwindUi/data/batchexecute?${qs}`;

  console.log("\n=== B/C. batchexecute ===");
  console.log("POST", url);
  const r = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/x-www-form-urlencoded;charset=UTF-8" },
    body: `f.req=${encodeURIComponent(fReq)}&at=${encodeURIComponent(at)}&`,
  });
  const text = await r.text();
  console.log("HTTP", r.status, r.headers.get("content-type"));
  console.log("body[0:800]:\n" + text.slice(0, 800));

  console.log("\n=== 判读 ===");
  if (r.status === 200 && text.includes(rpcid)) {
    console.log("✅ 协议一致 —— notebooklm-py 改个 base URL 就能用");
  } else if (r.status === 200) {
    console.log("⚠️  端点在,但没回显 " + rpcid + " —— RPC ID 被换过,需要重新抓包");
  } else if (r.status === 404) {
    console.log("❌ 这个路径不服务 batchexecute —— GE 前端不是同一套");
  } else {
    console.log("❌ HTTP " + r.status + " —— 见上面 body");
  }
})();
