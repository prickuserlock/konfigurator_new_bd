/* Toast notifications for BonusDostavkaBot
   - Converts inline .flash-inline notices to toasts
   - Allows programmatic showToast(message, type)
*/
(function(){
  function ensureContainer(){
    let c = document.getElementById("toast-container");
    if(!c){
      c = document.createElement("div");
      c.id = "toast-container";
      c.className = "toast-container";
      c.setAttribute("aria-live","polite");
      c.setAttribute("aria-atomic","true");
      document.body.appendChild(c);
    }
    return c;
  }

  function inferTypeFromEl(el){
    if(!el || !el.classList) return "info";
    if(el.classList.contains("error")) return "error";
    if(el.classList.contains("success")) return "success";
    if(el.classList.contains("warn")) return "warn";
    return "info";
  }

  function removeToast(toast){
    if(!toast) return;
    toast.classList.add("toast-out");
    // match CSS animation duration
    setTimeout(() => toast.remove(), 200);
  }

  function showToast(message, type, timeoutMs){
    if(!message) return;
    const c = ensureContainer();

    const toast = document.createElement("div");
    // support both `.toast.success` and `.toast.toast-success`
    const safeType = (type || "info").toLowerCase();
    toast.className = "toast " + safeType;

    const msg = document.createElement("div");
    msg.className = "toast-msg";
    msg.textContent = message;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toast-close";
    btn.setAttribute("aria-label", "Закрыть уведомление");
    btn.textContent = "×";

    btn.addEventListener("click", () => removeToast(toast));

    toast.appendChild(msg);
    toast.appendChild(btn);

    c.appendChild(toast);

    const t = (typeof timeoutMs === "number" ? timeoutMs : 4500);
    if(t > 0){
      setTimeout(() => removeToast(toast), t);
    }
  }

  // Expose for manual use if needed
  window.showToast = showToast;

  function boot(){
    // If page has inline flash notices, convert them to toasts.
    // We don't want duplicate messages, so remove the inline blocks.
    const flash = document.querySelectorAll(".flash-inline");
    let hadInline = false;
    flash.forEach((el) => {
      const text = (el.textContent || "").trim();
      if(!text) return;
      hadInline = true;
      showToast(text, inferTypeFromEl(el), 5000);
      el.remove();
    });

    // Если шаблон уже отрисовал flash-inline (обычно он же строится из msg/err),
    // то URL-параметры не дублируем.
    if(hadInline) return;

    // Fallback: если на странице нет flash-inline, но есть msg/err в URL — показываем.
    try{
      const url = new URL(window.location.href);
      const msg = url.searchParams.get("msg");
      const err = url.searchParams.get("err");
      if(msg) showToast(msg, "success", 5000);
      if(err) showToast(err, "error", 6000);
    }catch(e){}
  }

  if(document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", boot);
  }else{
    boot();
  }
})();
