/* AutoGrader frontend helpers — vanilla JS, no dependencies. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  var TERMINAL = {completed: 1, completed_with_warnings: 1, failed: 1, incomplete: 1};

  async function pollRun(runId) {
    var el = document.getElementById("run-progress");
    for (var i = 0; i < 240; i++) {           // ~8 minutes max
      await new Promise(function (r) { setTimeout(r, 2000); });
      try {
        var resp = await fetch("/api/evaluations/" + encodeURIComponent(runId));
        if (!resp.ok) continue;
        var data = await resp.json();
        if (el) el.innerHTML = "<p class=\"muted\">status: <b>" + esc(data.status) + "</b></p>";
        if (TERMINAL[data.status]) { location.reload(); return; }
      } catch (e) { /* keep polling */ }
    }
    if (el) el.innerHTML = "<p class=\"muted\">Still running after 8 minutes — check the dashboard later.</p>";
  }

  async function startEvaluation(formData) {
    var out = document.getElementById("eval-progress");
    out.innerHTML = "<p><span class=\"spin\"></span> Uploading files…</p>";

    async function upload(url, file, field) {
      if (!file || !file.size) return null;
      var fd = new FormData();
      fd.append("file", file);
      var resp = await fetch(url, {method: "POST", body: fd});
      var data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || ("upload failed: " + resp.status));
      return data.id;
    }

    try {
      var assignmentId = formData.get("assignment_id") ||
        await upload("/api/assignments", formData.get("assignment_file"));
      var submissionId = formData.get("submission_id") ||
        await upload("/api/submissions", formData.get("submission_file"));
      var rulesId = formData.get("rules_id");
      if (!assignmentId)
        throw new Error("Pick an existing assignment or upload an assignment file.");
      if (!submissionId)
        throw new Error("Pick an existing submission or upload a C file.");
      if (!rulesId)
        throw new Error("Pick grading rules (add them on the Rules page first).");

      out.innerHTML = "<p><span class=\"spin\"></span> Starting evaluation…</p>";
      var resp = await fetch("/api/evaluations", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({assignment_id: assignmentId, submission_id: submissionId, rules_id: rulesId})
      });
      var data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || ("failed: " + resp.status));
      location.href = "/runs/" + encodeURIComponent(data.run_id);
    } catch (e) {
      out.innerHTML = "<div class=\"flash err\">" + esc(e.message) + "</div>";
    }
  }

  window.AG = {pollRun: pollRun, startEvaluation: startEvaluation, esc: esc};
})();
