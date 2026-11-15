// Task detail live-updates: polls /api/tasks/<id> while the task is
// active and re-renders the step list, state badge and usage counters.
(function () {
  "use strict";
  var stepsEl = document.getElementById("steps");
  if (!stepsEl) return;
  var taskId = stepsEl.getAttribute("data-task");
  var activeStates = ["queued", "running", "waiting_approval"];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function render(data) {
    var stateEl = document.getElementById("task-state");
    if (stateEl) {
      stateEl.textContent = data.state;
      stateEl.className = "state " + data.state;
    }
    if (!data.steps || !data.steps.length) return;
    var html = "";
    data.steps.forEach(function (s) {
      var label = s.tool_name || (s.kind === "model_call" ? "model" : s.kind);
      var tok = s.tokens_in
        ? ' <span class="muted">in:' + s.tokens_in +
          " out:" + s.tokens_out + "</span>"
        : "";
      html += '<li class="step ' + esc(s.status) + '">' +
        '<span class="seq">' + s.seq + "</span> <strong>" + esc(label) +
        '</strong> <span class="state">' + esc(s.status) + "</span>" +
        tok + "</li>";
    });
    stepsEl.innerHTML = html;
    if (data.error_text) {
      var b = document.querySelector(".banner.error");
      if (!b) {
        b = document.createElement("div");
        b.className = "banner error";
        b.setAttribute("role", "alert");
        var h1 = document.querySelector("main h1");
        if (h1) h1.parentNode.insertBefore(b, h1.nextSibling);
      }
      b.textContent = data.error_text +
        (data.reset_at ? " Quota resets at " + data.reset_at + "." : "");
    }
  }

  var note = document.getElementById("live-note");
  var timer = setInterval(function () {
    fetch("/api/tasks/" + taskId, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        render(data);
        if (activeStates.indexOf(data.state) === -1) {
          clearInterval(timer);
          if (note) note.hidden = true;
          if (data.state === "succeeded" || data.state === "failed") {
            window.location.reload();
          }
        } else if (note) {
          note.hidden = false;
        }
      })
      .catch(function () { /* keep polling; transient errors are fine */ });
  }, 2000);
})();
